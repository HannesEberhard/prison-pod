import subprocess

import pytest

import manifests

SPEC = {
    "repos": [
        {"name": "api", "url": "https://h/api.git", "ref": "main"},
        {"name": "web", "url": "https://h/web.git"},
    ],
    "credentialsSecretRef": {"name": "claude-credentials"},
    "permissionMode": "acceptEdits",
}
IMAGE = "agent@sha256:" + "0" * 64


def pod_spec(spec=SPEC):
    return manifests.build_deployment("demo", spec, IMAGE)["spec"]["template"]["spec"]


def env_of(container):
    return {e["name"]: e["value"] for e in container["env"]}


def test_metadata_and_selector():
    d = manifests.build_deployment("demo", SPEC, IMAGE)
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
    assert env_of(agent) == {"SANDBOX_NAME": "demo", "PERMISSION_MODE": "acceptEdits"}
    assert agent["image"] == IMAGE
    assert agent["workingDir"] == "/data/workspace"


def test_one_clone_init_container_per_repo():
    clones = pod_spec()["initContainers"]
    assert [c["name"] for c in clones] == ["clone-api", "clone-web"]
    assert [c["command"][4] for c in clones] == ["/data/workspace/api", "/data/workspace/web"]


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
    pvc = manifests.build_pvc("demo", storage_class)
    assert pvc["metadata"]["name"] == "prison-pod-demo"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"].get("storageClassName") == expected


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
