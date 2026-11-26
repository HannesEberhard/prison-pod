import pytest

import status

SPEC = {"repos": [{"name": "api"}, {"name": "web"}]}
NOW = "2026-11-26T10:00:00Z"
EARLIER = "2026-11-26T09:00:00Z"


def pod(created="2026-11-26T08:00:00Z", deleting=False, **pod_status):
    metadata = {"name": f"p-{created}", "creationTimestamp": created}
    if deleting:
        metadata["deletionTimestamp"] = NOW
    return {"metadata": metadata, "status": pod_status}


def done(name):
    return {"name": name, "state": {"terminated": {"exitCode": 0, "reason": "Completed"}}}


def running(name):
    return {"name": name, "state": {"running": {"startedAt": NOW}}}


SCHEDULED = [{"type": "PodScheduled", "status": "True"}]


def test_current_pod_is_the_newest_that_is_not_terminating():
    old = pod("2026-11-26T08:00:00Z", deleting=True)
    new = pod("2026-11-26T07:00:00Z")
    assert status.current_pod([old, new]) is new
    assert status.current_pod([old]) is None
    assert status.current_pod([pod("2026-11-26T07:00:00Z"), newest := pod("2026-11-26T08:00:00Z")]) is newest


@pytest.mark.parametrize(
    ("the_pod", "expected"),
    [
        (None, (False, "PodPending", "no pod yet")),
        (
            pod(conditions=[{"type": "PodScheduled", "status": "False", "message": "0/3 nodes are available"}]),
            (False, "PodPending", "0/3 nodes are available"),
        ),
        (
            pod(conditions=SCHEDULED, initContainerStatuses=[
                done("clone-api"),
                {"name": "clone-web", "state": {"running": {}}},
            ]),
            (False, "Cloning", "clone-web"),
        ),
        (
            pod(conditions=SCHEDULED, initContainerStatuses=[
                {"name": "clone-api", "state": {"waiting": {"reason": "PodInitializing"}}},
            ]),
            (False, "Cloning", "clone-api"),
        ),
        (
            pod(conditions=SCHEDULED, initContainerStatuses=[
                {
                    "name": "clone-api",
                    "state": {"running": {}},
                    "lastState": {"terminated": {"exitCode": 128, "message": "fatal: unable to access"}},
                },
            ]),
            (False, "Cloning", "clone-api"),
        ),
        (
            pod(conditions=SCHEDULED, initContainerStatuses=[
                {
                    "name": "clone-api",
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 40s"}},
                    "lastState": {"terminated": {"exitCode": 128, "message": "fatal: repository not found\n"}},
                },
            ]),
            (False, "CloneFailed", "clone-api: fatal: repository not found"),
        ),
        (
            pod(conditions=SCHEDULED, initContainerStatuses=[
                {"name": "clone-api", "state": {"waiting": {"reason": "ErrImagePull", "message": "not found"}}},
            ]),
            (False, "CloneFailed", "clone-api: ErrImagePull: not found"),
        ),
        (
            pod(conditions=SCHEDULED, initContainerStatuses=[done("clone-api")], containerStatuses=[
                {"name": "agent", "state": {"waiting": {"reason": "ContainerCreating"}}},
            ]),
            (False, "PodPending", "starting containers"),
        ),
        (
            pod(conditions=SCHEDULED, initContainerStatuses=[done("clone-api")], containerStatuses=[
                {
                    "name": "agent",
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    "lastState": {"terminated": {"exitCode": 1, "reason": "Error"}},
                },
            ]),
            (False, "AgentNotRunning", "Error"),
        ),
        (
            pod(conditions=SCHEDULED, initContainerStatuses=[done("clone-api")], containerStatuses=[
                {"name": "agent", "state": {"waiting": {"reason": "CreateContainerConfigError",
                                                        "message": 'secret "claude-credentials" not found'}}},
            ]),
            (False, "AgentNotRunning", 'CreateContainerConfigError: secret "claude-credentials" not found'),
        ),
        (
            pod(conditions=SCHEDULED, initContainerStatuses=[done("clone-api")], containerStatuses=[
                running("agent"),
                {"name": "ssh", "state": {"waiting": {"reason": "CrashLoopBackOff"}}},
            ]),
            (True, "AgentRunning", ""),
        ),
    ],
)
def test_readiness(the_pod, expected):
    assert status.readiness(the_pod) == expected


def test_last_transition_time_moves_only_when_the_status_flips():
    previous = [{"type": "Ready", "status": "False", "reason": "Cloning", "lastTransitionTime": EARLIER}]
    assert status.ready_condition(False, "PodPending", "", previous, NOW)["lastTransitionTime"] == EARLIER
    assert status.ready_condition(True, "AgentRunning", "", previous, NOW)["lastTransitionTime"] == NOW
    assert status.ready_condition(False, "PodPending", "", [], NOW)["lastTransitionTime"] == NOW


def test_build_status():
    assert status.build_status(SPEC, 3, None, {}, NOW) == {
        "observedGeneration": 3,
        "conditions": [{
            "type": "Ready", "status": "False", "reason": "PodPending", "message": "no pod yet",
            "lastTransitionTime": NOW,
        }],
        "repos": "api,web",
    }
