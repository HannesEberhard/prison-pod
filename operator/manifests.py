"""Builds the objects a PrisonPod owns: a PVC and a single-replica Deployment running Claude Code Remote Control."""

from collections.abc import Mapping
from typing import Any

GROUP = "prisonpod.fdr2026.dev"
NAME_LABEL = f"{GROUP}/name"
STORAGE_SIZE = "5Gi"

AGENT_UID = 1000  # must match agent/Dockerfile
SSH_PORT = 2222  # must match agent/sshd_config
CREDENTIALS_DIR = "/secrets/claude"  # must match agent/entrypoint.sh
# One PVC mounted at DATA_DIR; the image puts HOME at /data/home. No subPath mounts:
# kubelet would create those dirs as root.
DATA_DIR = "/data"
WORKSPACE_DIR = f"{DATA_DIR}/workspace"

CONTAINER_SECURITY = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}


def resource_name(name: str) -> str:
    return f"prison-pod-{name}"


def labels(name: str) -> dict[str, str]:
    return {NAME_LABEL: name}


def clone_command(repo: Mapping[str, str]) -> list[str]:
    """Clones unless a previous pod already did; arguments are passed positionally, never interpolated."""
    branch = ["--branch", repo["ref"]] if repo.get("ref") else []
    script = 'dir="$1"; shift; [ -d "$dir/.git" ] || exec git clone "$@" "$dir"'
    return ["sh", "-c", script, "clone", f"{WORKSPACE_DIR}/{repo['name']}", *branch, "--", repo["url"]]


def build_pvc(name: str, storage_class: str) -> dict:
    spec = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": STORAGE_SIZE}}}
    if storage_class:
        spec["storageClassName"] = storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": resource_name(name), "labels": labels(name)},
        "spec": spec,
    }


def build_deployment(name: str, spec: Mapping[str, Any], image: str) -> dict:
    data_mount = {"name": "data", "mountPath": DATA_DIR}

    clones = [
        {
            "name": f"clone-{repo['name']}",
            "image": image,
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
        "image": image,
        "workingDir": WORKSPACE_DIR,
        "env": [
            {"name": "SANDBOX_NAME", "value": name},
            {"name": "PERMISSION_MODE", "value": spec.get("permissionMode", "auto")},
        ],
        "volumeMounts": [
            data_mount,
            {"name": "claude-credentials", "mountPath": CREDENTIALS_DIR, "readOnly": True},
        ],
        "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}},
        "securityContext": CONTAINER_SECURITY,
    }
    containers = [agent]
    if keys := spec.get("ssh", {}).get("authorizedKeys"):
        containers.append(
            {
                "name": "ssh",
                "image": image,
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

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": resource_name(name), "labels": labels(name)},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},  # never two Remote Control sessions side by side
            "selector": {"matchLabels": labels(name)},
            "template": {
                "metadata": {"labels": labels(name)},
                "spec": {
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
                },
            },
        },
    }
