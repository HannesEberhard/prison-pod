import json
import logging

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
    "metadata": {"name": "demo", "namespace": "ns", "uid": "uid-1"},
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

    def __init__(self, pvc=None):
        self.pvc = pvc
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


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(main, "AGENT_IMAGE", "agent@" + DIGEST)

    def _run(api):
        monkeypatch.setattr(main.kubernetes.client, "CoreV1Api", lambda: api)
        monkeypatch.setattr(main.kubernetes.client, "AppsV1Api", lambda: api)
        main.reconcile(body=BODY, spec=SPEC, name="demo", logger=logging.getLogger("test"))

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


def test_reconcile_propagates_api_errors(run):
    class Failing(FakeApi):
        def read_namespaced_persistent_volume_claim(self, name, namespace):
            raise ApiException(status=403)

    with pytest.raises(ApiException):
        run(Failing())
