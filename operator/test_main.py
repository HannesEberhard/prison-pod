import json
import logging
from types import SimpleNamespace

import kopf
import pytest
import urllib3
from kubernetes.client import ApiClient, AppsV1Api, Configuration
from kubernetes.client.rest import ApiException

import main

SPEC = {
    "repos": [{"name": "api", "url": "https://h/api.git"}],
    "credentialsSecretRef": {"name": "claude-credentials"},
}
BODY = {
    "apiVersion": f"{main.GROUP}/v1alpha1",
    "kind": "PrisonPod",
    "metadata": {"name": "demo", "namespace": "ns", "uid": "uid-1", "generation": 2},
    "spec": SPEC,
}
DIGEST = "sha256:" + "0" * 64


@pytest.mark.parametrize(
    ("image", "pinned"),
    [("r/i@" + DIGEST, True), ("r/i:tag@" + DIGEST, True), ("r/i:tag", False), ("", False)],
)
def test_is_pinned_by_digest(image, pinned):
    assert main.is_pinned_by_digest(image) is pinned


def test_apply_deployment_sends_a_server_side_apply_request(monkeypatch):
    """Goes through the real client down to the HTTP pool, so the request is what the API server sees."""
    requests = []

    def request(method, url, body=None, headers=None, **_):
        requests.append((method, url, body, headers))
        return urllib3.HTTPResponse(
            body=b'{"kind": "Deployment", "apiVersion": "apps/v1", "spec": {"selector": {}, "template": {}}}',
            status=200, headers={"Content-Type": "application/json"}, preload_content=True,
        )

    api_client = ApiClient(Configuration(host="https://k8s"))
    monkeypatch.setattr(api_client.rest_client.pool_manager, "request", request)
    manifest = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "prison-pod-demo"}}

    main.apply_deployment(AppsV1Api(api_client), manifest, BODY)

    [(method, url, body, headers)] = requests
    assert method == "PATCH"
    assert url == (
        "https://k8s/apis/apps/v1/namespaces/ns/deployments/prison-pod-demo"
        "?fieldManager=prison-pod-operator&force=true"
    )
    assert headers["Content-Type"] == "application/apply-patch+yaml"
    sent = json.loads(body)
    assert sent["metadata"]["namespace"] == "ns"
    assert sent["metadata"]["ownerReferences"][0]["uid"] == "uid-1"


class FakeApi:
    """Stands in for both CoreV1Api (PVCs) and AppsV1Api (Deployments)."""

    def __init__(self, pvc=None, pods=(), log=""):
        self.pvc = pvc
        self.pods = list(pods)
        self.log = log
        self.created = []
        self.applied = []

    def read_namespaced_persistent_volume_claim(self, name, namespace):
        if self.pvc is None:
            raise ApiException(status=404)
        return self.pvc

    def create_namespaced_persistent_volume_claim(self, namespace, body):
        self.created.append(body)
        self.pvc = body
        return body

    def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        self.applied.append((name, namespace, body, kwargs))

    def list_namespaced_pod(self, namespace, label_selector, _preload_content):
        assert (namespace, label_selector, _preload_content) == ("ns", f"{main.NAME_LABEL}=demo", False)
        return SimpleNamespace(data=json.dumps({"items": self.pods}).encode())

    def read_namespaced_pod_log(self, name, namespace, container, limit_bytes):
        assert (name, namespace, container) == ("p", "ns", "agent")
        return self.log


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(main, "AGENT_IMAGE", "agent@" + DIGEST)

    def _run(api, status=None):
        monkeypatch.setattr(main.kubernetes.client, "CoreV1Api", lambda: api)
        monkeypatch.setattr(main.kubernetes.client, "AppsV1Api", lambda: api)
        patch = kopf.Patch()
        main.reconcile(
            body=BODY, spec=SPEC, meta=BODY["metadata"], name="demo", namespace="ns",
            status=status or {}, patch=patch, logger=logging.getLogger("test"),
        )
        return patch

    return _run


def test_reconcile_creates_the_pvc_once_and_applies_the_deployment_every_time(run):
    api = FakeApi()
    run(api)
    run(api)
    [pvc] = api.created
    assert pvc["metadata"]["ownerReferences"][0]["uid"] == "uid-1"
    assert len(api.applied) == 2
    name, namespace, deployment, kwargs = api.applied[-1]
    assert (name, namespace) == ("prison-pod-demo", "ns")
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == "agent@" + DIGEST
    assert kwargs["field_manager"] == "prison-pod-operator"
    assert kwargs["force"] is True


RUNNING_POD = {
    "metadata": {"name": "p", "creationTimestamp": "2026-11-26T08:00:00Z"},
    "status": {"containerStatuses": [{"name": "agent", "state": {"running": {}}}]},
}


def test_reconcile_reports_the_pod_and_session_in_status(run):
    url = "https://claude.ai/code/session_01AbC"
    patch = run(FakeApi(pods=[RUNNING_POD], log=f"Session: {url}\n"))
    assert patch.status["observedGeneration"] == 2
    assert patch.status["repos"] == "api"
    assert patch.status["sessionURL"] == url
    [ready] = patch.status["conditions"]
    assert (ready["status"], ready["reason"]) == ("True", "SessionRegistered")


def test_reconcile_skips_unchanged_status(run):
    api = FakeApi(pods=[RUNNING_POD])
    current = dict(run(api).status)
    assert run(api, status=current) == {}


def test_reconcile_propagates_api_errors(run):
    class Failing(FakeApi):
        def read_namespaced_persistent_volume_claim(self, name, namespace):
            raise ApiException(status=403)

    with pytest.raises(ApiException):
        run(Failing())
