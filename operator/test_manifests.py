import subprocess

import pytest

import manifests

SPEC = {
    "repos": [
        {"name": "api", "url": "https://h/api.git", "ref": "main"},
        {"name": "web", "url": "https://h/web.git"},
    ],
    "credentialsSecretRef": {"name": "claude-credentials"},
    "git": {"name": "Ada Lovelace", "email": "ada@example.com"},
    "permissionMode": "acceptEdits",
}
IMAGE = "agent@sha256:" + "0" * 64
SETTINGS = manifests.Settings(image=IMAGE)


def pod_spec(spec=SPEC):
    return manifests.build_deployment("demo", spec, SETTINGS)["spec"]["template"]["spec"]


def env_of(container):
    return {e["name"]: e["value"] for e in container["env"]}


def test_metadata_and_selector():
    d = manifests.build_deployment("demo", SPEC, SETTINGS)
    labels = {manifests.NAME_LABEL: "demo"}
    assert d["metadata"]["name"] == "prison-pod-demo"
    assert d["spec"]["selector"]["matchLabels"] == labels
    assert d["spec"]["template"]["metadata"]["labels"] == labels
    assert d["spec"]["strategy"]["type"] == "Recreate"


def run_clone(repo, workspace, monkeypatch):
    """Runs the real clone script with a stub `git` that records its args and fakes a checkout."""
    bin_dir = workspace / "bin"
    bin_dir.mkdir(exist_ok=True)
    git = bin_dir / "git"
    git.write_text('#!/bin/sh\necho "$@" >> "$GIT_LOG"\nfor a; do last="$a"; done\nmkdir -p "$last/.git"\n')
    git.chmod(0o755)
    monkeypatch.setattr(manifests, "WORKSPACE_DIR", str(workspace))
    log = workspace / "git.log"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "GIT_LOG": str(log)}
    subprocess.run(manifests.clone_command(repo), env=env, check=True, cwd=workspace)
    return log.read_text().splitlines() if log.exists() else []


def test_clone_passes_ref_and_target(tmp_path, monkeypatch):
    calls = run_clone({"name": "r", "url": "https://h/r.git", "ref": "v1"}, tmp_path, monkeypatch)
    assert calls == [f"clone --branch v1 -- https://h/r.git {tmp_path}/r"]


def test_clone_is_skipped_when_already_cloned(tmp_path, monkeypatch):
    repo = {"name": "r", "url": "https://h/r.git"}
    run_clone(repo, tmp_path, monkeypatch)
    assert len(run_clone(repo, tmp_path, monkeypatch)) == 1


def test_clone_does_not_evaluate_url(tmp_path, monkeypatch):
    run_clone({"name": "r", "url": "https://h/$(touch pwned).git"}, tmp_path, monkeypatch)
    assert not (tmp_path / "pwned").exists()


def test_agent_container():
    agent = pod_spec()["containers"][0]
    assert env_of(agent) == {
        "SANDBOX_NAME": "demo", "PERMISSION_MODE": "acceptEdits",
        "GIT_USER_NAME": "Ada Lovelace", "GIT_USER_EMAIL": "ada@example.com",
    }
    assert agent["image"] == IMAGE
    assert agent["workingDir"] == "/data/workspace"


def test_one_clone_init_container_per_repo():
    clones = pod_spec()["initContainers"]
    assert [c["name"] for c in clones] == ["clone-api", "clone-web"]
    assert [c["command"][4] for c in clones] == ["/data/workspace/api", "/data/workspace/web"]
    assert {c["terminationMessagePolicy"] for c in clones} == {"FallbackToLogsOnError"}


def test_no_ssh_sidecar_without_keys():
    assert [c["name"] for c in pod_spec()["containers"]] == ["agent"]


def test_ssh_sidecar():
    spec = {**SPEC, "ssh": {"authorizedKeys": ["ssh-ed25519 AAAA one", "ssh-ed25519 BBBB two"]}}
    ssh = pod_spec(spec)["containers"][1]
    assert ssh["name"] == "ssh"
    assert ssh["command"] == ["/usr/local/bin/sshd.sh"]
    assert env_of(ssh)["SSH_AUTHORIZED_KEYS"] == "ssh-ed25519 AAAA one\nssh-ed25519 BBBB two"
    assert {"name": "data", "mountPath": "/data"} in ssh["volumeMounts"]
    assert ssh["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_data_volume_is_the_pvc():
    volumes = {v["name"]: v for v in pod_spec()["volumes"]}
    assert volumes["data"]["persistentVolumeClaim"]["claimName"] == "prison-pod-demo"


@pytest.mark.parametrize(("storage_class", "expected"), [("local-path", "local-path"), ("", None)])
def test_build_pvc(storage_class, expected):
    pvc = manifests.build_pvc("demo", manifests.Settings(image=IMAGE, storage_class=storage_class, storage_size="9Gi"))
    assert pvc["metadata"]["name"] == "prison-pod-demo"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "9Gi"
    assert pvc["spec"].get("storageClassName") == expected


def test_agent_resources_come_from_settings():
    resources = {"limits": {"memory": "8Gi"}}
    d = manifests.build_deployment("demo", SPEC, manifests.Settings(image=IMAGE, agent_resources=resources))
    assert d["spec"]["template"]["spec"]["containers"][0]["resources"] == resources


def test_permission_mode_defaults():
    spec = {k: v for k, v in SPEC.items() if k != "permissionMode"}
    assert env_of(pod_spec(spec)["containers"][0])["PERMISSION_MODE"] == "auto"


def test_credentials_secret_and_security():
    ps = pod_spec()
    secret = next(v for v in ps["volumes"] if v["name"] == "claude-credentials")
    assert secret["secret"]["secretName"] == "claude-credentials"
    assert ps["securityContext"]["runAsNonRoot"] is True
    assert ps["automountServiceAccountToken"] is False
    for c in ps["initContainers"] + ps["containers"]:
        assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_runtime_class_is_set_only_when_configured():
    assert "runtimeClassName" not in pod_spec()
    d = manifests.build_deployment("demo", SPEC, manifests.Settings(image=IMAGE, runtime_class="gvisor"))
    assert d["spec"]["template"]["spec"]["runtimeClassName"] == "gvisor"


def test_network_policy_selects_the_sandbox_and_denies_by_default():
    np = manifests.build_network_policy("demo", SPEC, SETTINGS)
    assert (np["apiVersion"], np["kind"]) == ("cilium.io/v2", "CiliumNetworkPolicy")
    assert np["metadata"]["name"] == "prison-pod-demo"
    assert np["spec"]["endpointSelector"] == {"matchLabels": {manifests.NAME_LABEL: "demo"}}
    assert np["spec"]["enableDefaultDeny"] == {"ingress": True, "egress": True}
    assert "ingress" not in np["spec"]


def test_network_policy_allows_dns_through_the_proxy():
    dns, _ = manifests.build_network_policy("demo", SPEC, SETTINGS)["spec"]["egress"]
    assert dns["toEndpoints"][0]["matchLabels"]["k8s:k8s-app"] == "kube-dns"
    assert dns["toPorts"][0]["rules"] == {"dns": [{"matchPattern": "*"}]}


def test_network_policy_allows_essential_and_repo_hosts_on_443():
    spec = {**SPEC, "repos": [{"name": "a", "url": "https://GitHub.com/o/a.git"}, {"name": "b", "url": "https://h/b.git"}]}
    settings = manifests.Settings(image=IMAGE, essential_hosts=("claude.ai", "*.anthropic.com"))
    _, https = manifests.build_network_policy("demo", spec, settings)["spec"]["egress"]
    assert https["toFQDNs"] == [
        {"matchPattern": "*.anthropic.com"}, {"matchName": "claude.ai"}, {"matchName": "github.com"}, {"matchName": "h"},
    ]
    assert https["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]


def test_network_policy_allows_spec_egress_grouped_by_ports():
    spec = {**SPEC, "egress": [
        {"host": "registry.npmjs.org"},
        {"host": "github.com", "ports": [22, 443]},
        {"host": "*.githubusercontent.com", "ports": [443]},
        {"host": "gitlab.com", "ports": [443, 22]},
    ]}
    settings = manifests.Settings(image=IMAGE, essential_hosts=("claude.ai",))
    _, *allowed = manifests.build_network_policy("demo", spec, settings)["spec"]["egress"]
    assert allowed == [
        {
            "toFQDNs": [{"matchName": "github.com"}, {"matchName": "gitlab.com"}],
            "toPorts": [{"ports": [{"port": "22", "protocol": "TCP"}, {"port": "443", "protocol": "TCP"}]}],
        },
        {
            "toFQDNs": [
                {"matchPattern": "*.githubusercontent.com"}, {"matchName": "claude.ai"}, {"matchName": "h"},
                {"matchName": "registry.npmjs.org"},
            ],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        },
    ]


def test_git_credentials_reach_clones_agent_and_ssh():
    spec = {**SPEC, "gitCredentialsSecretRef": {"name": "gh"}, "ssh": {"authorizedKeys": ["ssh-ed25519 AAAA"]}}
    pod = pod_spec(spec)
    for c in [*pod["initContainers"], *pod["containers"]]:
        assert {"name": "GIT_CONFIG_VALUE_0", "value": manifests.GIT_HELPER} in c["env"]
        assert {"name": "git-credentials", "mountPath": "/secrets/git", "readOnly": True} in c["volumeMounts"]
    assert {"name": "git-credentials", "secret": {"secretName": "gh", "defaultMode": 0o440}} in pod["volumes"]


def test_no_git_credentials_by_default():
    pod = pod_spec()
    assert all(v["name"] != "git-credentials" for v in pod["volumes"])
    assert not any("GIT_CONFIG_COUNT" in str(c.get("env")) for c in pod["initContainers"] + pod["containers"])


def run_helper(tmp_path, stdin, files):
    """Runs the real credential helper against a fake secret directory."""
    for k, v in files.items():
        (tmp_path / k).write_text(v)
    script = open("../agent/git-credential-secret").read().replace("dir=/secrets/git", f"dir={tmp_path}")
    return subprocess.run(["bash", "-c", script, "helper", "get"], input=stdin, text=True, capture_output=True, check=True).stdout


def test_credential_helper_only_answers_for_its_host(tmp_path):
    ok = run_helper(tmp_path, "protocol=https\nhost=github.com\n\n", {"token": "s3cret\n"})
    assert ok == "username=x-access-token\npassword=s3cret\n"
    assert run_helper(tmp_path, "protocol=https\nhost=evil.example\n\n", {}) == ""
    assert run_helper(tmp_path, "protocol=http\nhost=github.com\n\n", {}) == ""
    custom = run_helper(tmp_path, "protocol=https\nhost=gl.example\n\n", {"host": "gl.example", "username": "oauth2"})
    assert custom == "username=oauth2\npassword=s3cret\n"
