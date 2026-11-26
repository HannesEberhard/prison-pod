"""Reconciles each PrisonPod into a PVC and a single-replica Deployment running Claude Code Remote Control."""

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

import kopf
import kubernetes
from kubernetes.client.rest import ApiException

from manifests import GROUP, NAME_LABEL, Settings, build_deployment, build_pvc
from status import agent_running, build_status, current_pod


def settings_from_env(env: Mapping[str, str]) -> Settings:
    """Unset or empty variables keep the defaults."""
    settings = Settings(image=env.get("AGENT_IMAGE", ""), storage_class=env.get("STORAGE_CLASS", ""))
    if size := env.get("STORAGE_SIZE"):
        settings = replace(settings, storage_size=size)
    if resources := env.get("AGENT_RESOURCES"):
        settings = replace(settings, agent_resources=json.loads(resources))
    return settings


SETTINGS = settings_from_env(os.environ)
FIELD_MANAGER = "prison-pod-operator"
# Remote Control prints the session URL right after starting.
LOG_HEAD_BYTES = 64 * 1024


def is_pinned_by_digest(image: str) -> bool:
    return re.search(r"@sha256:[a-f0-9]{64}$", image) is not None


@kopf.on.startup()
def startup(settings: kopf.OperatorSettings, **_):
    if not is_pinned_by_digest(SETTINGS.image):
        raise kopf.PermanentError(f"AGENT_IMAGE must be pinned by digest, got {SETTINGS.image!r}")
    # By default every INFO log line (e.g. each timer run) becomes a Kubernetes Event.
    settings.posting.level = logging.WARNING
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:  # running locally
        kubernetes.config.load_kube_config()


def ensure(read, create, manifest: dict, owner, logger):
    """Returns the live object, creating `manifest` (owned by `owner`) if it doesn't exist."""
    kopf.adopt(manifest, owner=owner)
    name, namespace = manifest["metadata"]["name"], manifest["metadata"]["namespace"]
    try:
        return read(name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise
    logger.info(f"Creating {manifest['kind']} {name}")
    return create(namespace, manifest)


def apply_deployment(apps, manifest: dict, owner) -> None:
    """Creates or converges the Deployment (owned by `owner`) via server-side apply; a no-op if nothing changed."""
    kopf.adopt(manifest, owner=owner)
    apps.patch_namespaced_deployment(
        manifest["metadata"]["name"],
        manifest["metadata"]["namespace"],
        manifest,
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
    )


# Timers fire as soon as a PrisonPod is seen, so no separate create handler is needed.
@kopf.timer(GROUP, "prisonpods", interval=10)
def reconcile(body, spec, meta, name, namespace, status, patch, logger, **_):
    core, apps = kubernetes.client.CoreV1Api(), kubernetes.client.AppsV1Api()
    # Created once and never updated: it holds the data, and most PVC fields are immutable.
    ensure(
        core.read_namespaced_persistent_volume_claim,
        core.create_namespaced_persistent_volume_claim,
        build_pvc(name, SETTINGS), body, logger,
    )
    apply_deployment(apps, build_deployment(name, spec, SETTINGS), body)

    # Plain API JSON: what status.py works on, and what the tests can write by hand.
    pods = json.loads(core.list_namespaced_pod(
        namespace, label_selector=f"{NAME_LABEL}={name}", _preload_content=False,
    ).data)["items"]
    pod = current_pod(pods)
    agent_log = ""
    if agent_running(pod):
        agent_log = core.read_namespaced_pod_log(
            pod["metadata"]["name"], namespace, container="agent", limit_bytes=LOG_HEAD_BYTES,
        )
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    desired = build_status(spec, meta["generation"], pod, agent_log, status, now)
    if any(status.get(key) != value for key, value in desired.items()):
        patch.status.update(desired)
