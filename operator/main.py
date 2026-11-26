"""Reconciles each PrisonPod into a PVC and a single-replica Deployment running Claude Code Remote Control."""

import logging
import os
import re

import kopf
import kubernetes
from kubernetes.client.rest import ApiException

from manifests import GROUP, build_deployment, build_pvc

AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "")
STORAGE_CLASS = os.environ.get("STORAGE_CLASS", "")  # empty: cluster default
FIELD_MANAGER = "prison-pod-operator"


def is_pinned_by_digest(image: str) -> bool:
    return re.search(r"@sha256:[a-f0-9]{64}$", image) is not None


@kopf.on.startup()
def startup(settings: kopf.OperatorSettings, **_):
    if not is_pinned_by_digest(AGENT_IMAGE):
        raise kopf.PermanentError(f"AGENT_IMAGE must be pinned by digest, got {AGENT_IMAGE!r}")
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
def reconcile(body, spec, name, logger, **_):
    core, apps = kubernetes.client.CoreV1Api(), kubernetes.client.AppsV1Api()
    # Created once and never updated: it holds the data, and most PVC fields are immutable.
    ensure(
        core.read_namespaced_persistent_volume_claim,
        core.create_namespaced_persistent_volume_claim,
        build_pvc(name, STORAGE_CLASS), body, logger,
    )
    apply_deployment(apps, build_deployment(name, spec, AGENT_IMAGE), body)
