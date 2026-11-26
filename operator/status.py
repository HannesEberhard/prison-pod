"""Derives a PrisonPod's status from its pod (plain API JSON, as returned by the pods list)."""

from collections.abc import Mapping
from typing import Any

Pod = Mapping[str, Any]


def current_pod(pods: list[Pod]) -> Pod | None:
    """The newest pod that isn't terminating; during a Recreate rollout the old one may still be shutting down."""
    live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
    return max(live, key=lambda p: p["metadata"]["creationTimestamp"], default=None)


def _failure(container_status: Mapping[str, Any]) -> str | None:
    """Why the container failed, if it did: it exited non-zero now or last time, or can't be started."""
    state, last = container_status.get("state", {}), container_status.get("lastState", {})
    if "running" in state:  # a retry in progress
        return None
    terminated = state.get("terminated") or last.get("terminated")
    if terminated and terminated.get("exitCode") != 0:
        return (terminated.get("message") or terminated.get("reason") or "").strip() or f"exit code {terminated['exitCode']}"
    waiting = state.get("waiting")
    if waiting and waiting.get("reason") not in (None, "ContainerCreating", "PodInitializing"):
        return f"{waiting['reason']}: {waiting.get('message', '')}".rstrip(": ")
    return None


def readiness(pod: Pod | None) -> tuple[bool, str, str]:
    """(ready, reason, message) for the Ready condition."""
    if pod is None:
        return False, "PodPending", "no pod yet"
    pod_status = pod.get("status", {})
    scheduled = next((c for c in pod_status.get("conditions", []) if c["type"] == "PodScheduled"), None)
    if scheduled and scheduled["status"] != "True":
        return False, "PodPending", scheduled.get("message", "")

    for init in pod_status.get("initContainerStatuses", []):
        if failure := _failure(init):
            return False, "CloneFailed", f"{init['name']}: {failure}"
        if init.get("state", {}).get("terminated", {}).get("exitCode") != 0:
            return False, "Cloning", init["name"]

    agent = next((c for c in pod_status.get("containerStatuses", []) if c["name"] == "agent"), None)
    if agent is None or "running" not in agent.get("state", {}):
        failure = agent and _failure(agent)
        if failure:
            return False, "AgentNotRunning", failure
        return False, "PodPending", "starting containers"
    return True, "AgentRunning", ""


def ready_condition(ready: bool, reason: str, message: str, previous: list[Mapping[str, Any]], now: str) -> dict:
    """The Ready condition; lastTransitionTime only moves when the status flips."""
    status = "True" if ready else "False"
    old = next((c for c in previous if c.get("type") == "Ready"), {})
    since = old["lastTransitionTime"] if old.get("status") == status and old.get("lastTransitionTime") else now
    return {"type": "Ready", "status": status, "reason": reason, "message": message, "lastTransitionTime": since}


def build_status(
    spec: Mapping[str, Any], generation: int, pod: Pod | None, previous: Mapping[str, Any], now: str,
) -> dict:
    return {
        "observedGeneration": generation,
        "conditions": [ready_condition(*readiness(pod), previous.get("conditions", []), now)],
        # Printer columns show only the first match of a list JSONPath, so join the names here.
        "repos": ",".join(repo["name"] for repo in spec["repos"]),
    }
