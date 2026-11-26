"""Builds the objects a PrisonPod owns: a PVC, a Cilium network policy and a single-replica Deployment
running Claude Code Remote Control."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

GROUP = "prisonpod.fdr2026.dev"
NAME_LABEL = f"{GROUP}/name"

AGENT_UID = 1000  # must match agent/Dockerfile
SSH_PORT = 2222  # must match agent/sshd_config
CREDENTIALS_DIR = "/secrets/claude"  # must match agent/entrypoint.sh
# One PVC mounted at DATA_DIR; the image puts HOME at /data/home. No subPath mounts:
# kubelet would create those dirs as root.
DATA_DIR = "/data"
WORKSPACE_DIR = f"{DATA_DIR}/workspace"

CONTAINER_SECURITY = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}


@dataclass(frozen=True)
class Settings:
    """Operator-wide settings for every sandbox, set through the Helm chart."""

    image: str
    storage_class: str = ""  # empty: cluster default
    storage_size: str = "5Gi"
    agent_resources: Mapping[str, Any] = field(
        default_factory=lambda: {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}}
    )
    runtime_class: str = ""  # empty: the cluster's default runtime
    # What Remote Control needs; reachable on 443 from every sandbox.
    essential_hosts: tuple[str, ...] = ("api.anthropic.com", "claude.ai", "claude.com", "platform.claude.com")


def resource_name(name: str) -> str:
    return f"prison-pod-{name}"


def labels(name: str) -> dict[str, str]:
    return {NAME_LABEL: name}


def clone_command(repo: Mapping[str, str]) -> list[str]:
    """Clones unless a previous pod already did; arguments are passed positionally, never interpolated."""
    branch = ["--branch", repo["ref"]] if repo.get("ref") else []
    script = 'dir="$1"; shift; [ -d "$dir/.git" ] || exec git clone "$@" "$dir"'
    return ["sh", "-c", script, "clone", f"{WORKSPACE_DIR}/{repo['name']}", *branch, "--", repo["url"]]


def build_pvc(name: str, settings: Settings) -> dict:
    spec = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": settings.storage_size}}}
    if settings.storage_class:
        spec["storageClassName"] = settings.storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": resource_name(name), "labels": labels(name)},
        "spec": spec,
    }


def build_deployment(name: str, spec: Mapping[str, Any], settings: Settings) -> dict:
    data_mount = {"name": "data", "mountPath": DATA_DIR}

    clones = [
        {
            "name": f"clone-{repo['name']}",
            "image": settings.image,
            "command": clone_command(repo),
            # Puts git's error into the container status, which the Ready condition reports.
            "terminationMessagePolicy": "FallbackToLogsOnError",
            "volumeMounts": [data_mount],
            "securityContext": CONTAINER_SECURITY,
        }
        for repo in spec["repos"]
    ]
    agent = {
        "name": "agent",
        "image": settings.image,
        "workingDir": WORKSPACE_DIR,
        "env": [
            {"name": "SANDBOX_NAME", "value": name},
            {"name": "PERMISSION_MODE", "value": spec.get("permissionMode", "auto")},
            {"name": "GIT_USER_NAME", "value": spec["git"]["name"]},
            {"name": "GIT_USER_EMAIL", "value": spec["git"]["email"]},
        ],
        "volumeMounts": [
            data_mount,
            {"name": "claude-credentials", "mountPath": CREDENTIALS_DIR, "readOnly": True},
        ],
        "resources": settings.agent_resources,
        "securityContext": CONTAINER_SECURITY,
    }
    containers = [agent]
    if keys := spec.get("ssh", {}).get("authorizedKeys"):
        containers.append(
            {
                "name": "ssh",
                "image": settings.image,
                "command": ["/usr/local/bin/sshd.sh"],
                "env": [
                    {"name": "SANDBOX_NAME", "value": name},
                    {"name": "SSH_AUTHORIZED_KEYS", "value": "\n".join(keys)},
                ],
                "ports": [{"name": "ssh", "containerPort": SSH_PORT}],
                "volumeMounts": [data_mount],
                # SSH sessions (e.g. a VS Code server) run in this container.
                "resources": {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"memory": "1Gi"}},
                "securityContext": CONTAINER_SECURITY,
            }
        )
    volumes = [
        {"name": "data", "persistentVolumeClaim": {"claimName": resource_name(name)}},
        {
            "name": "claude-credentials",
            "secret": {"secretName": spec["credentialsSecretRef"]["name"], "defaultMode": 0o440},
        },
    ]

    pod_spec = {
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": AGENT_UID,
            "runAsGroup": AGENT_UID,
            "fsGroup": AGENT_UID,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "initContainers": clones,
        "containers": containers,
        "volumes": volumes,
    }
    if settings.runtime_class:
        pod_spec["runtimeClassName"] = settings.runtime_class

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": resource_name(name), "labels": labels(name)},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},  # never two Remote Control sessions side by side
            "selector": {"matchLabels": labels(name)},
            "template": {"metadata": {"labels": labels(name)}, "spec": pod_spec},
        },
    }


def _fqdns(hosts: Iterable[str]) -> list[dict[str, str]]:
    return [{"matchPattern": h} if h.startswith("*.") else {"matchName": h} for h in sorted(set(hosts))]


def _tcp(ports: Iterable[int]) -> list[dict]:
    return [{"ports": [{"port": str(p), "protocol": "TCP"} for p in sorted(set(ports))]}]


def build_network_policy(name: str, spec: Mapping[str, Any], settings: Settings) -> dict:
    """Default deny in both directions; egress only to DNS, the essential and repo hosts, and spec.egress."""
    hosts_by_ports: dict[tuple[int, ...], list[str]] = defaultdict(list)
    hosts_by_ports[(443,)] += settings.essential_hosts
    hosts_by_ports[(443,)] += [urlsplit(repo["url"]).hostname for repo in spec["repos"]]
    for entry in spec.get("egress", []):
        hosts_by_ports[tuple(sorted(set(entry.get("ports", [443]))))].append(entry["host"])
    dns = {
        "toEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s:k8s-app": "kube-dns"}}],
        # Lookups go through Cilium's DNS proxy, which is how toFQDNs learns the IPs behind a name.
        "toPorts": [{"ports": [{"port": "53", "protocol": "ANY"}], "rules": {"dns": [{"matchPattern": "*"}]}}],
    }
    allowed = [{"toFQDNs": _fqdns(hosts), "toPorts": _tcp(ports)} for ports, hosts in sorted(hosts_by_ports.items())]
    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": resource_name(name), "labels": labels(name)},
        "spec": {
            "endpointSelector": {"matchLabels": labels(name)},
            # Explicit, because without any ingress rules Cilium would leave ingress open.
            "enableDefaultDeny": {"ingress": True, "egress": True},
            "egress": [dns, *allowed],
        },
    }
