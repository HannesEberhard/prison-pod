"""Derives a PrisonPod's status from its pod (plain API JSON, as returned by the pods list)."""

import re
from collections.abc import Mapping
from typing import Any

Pod = Mapping[str, Any]
SESSION_URL = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]+")


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


def _agent(pod: Pod | None) -> Mapping[str, Any] | None:
    statuses = (pod or {}).get("status", {}).get("containerStatuses", [])
    return next((c for c in statuses if c["name"] == "agent"), None)


def agent_running(pod: Pod | None) -> bool:
    return "running" in ((_agent(pod) or {}).get("state") or {})


def session_url(agent_log: str) -> str:
    """The last session URL Remote Control printed, or ''."""
    urls = SESSION_URL.findall(agent_log)
    return urls[-1] if urls else ""


def readiness(pod: Pod | None, url: str) -> tuple[bool, str, str]:
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

    if not agent_running(pod):
        if failure := _failure(_agent(pod) or {}):
            return False, "AgentNotRunning", failure
        return False, "PodPending", "starting containers"
    if not url:
        return False, "WaitingForSession", "Remote Control has not printed a session URL yet"
    return True, "SessionRegistered", ""


def ready_condition(ready: bool, reason: str, message: str, previous: list[Mapping[str, Any]], now: str) -> dict:
    """The Ready condition; lastTransitionTime only moves when the status flips."""
    status = "True" if ready else "False"
    old = next((c for c in previous if c.get("type") == "Ready"), {})
    since = old["lastTransitionTime"] if old.get("status") == status and old.get("lastTransitionTime") else now
    return {"type": "Ready", "status": status, "reason": reason, "message": message, "lastTransitionTime": since}


def build_status(
    spec: Mapping[str, Any], generation: int, pod: Pod | None, agent_log: str, previous: Mapping[str, Any], now: str,
) -> dict:
    """`agent_log` is the start of the running agent's log, or '' if it isn't running."""
    url = session_url(agent_log) if agent_running(pod) else ""
    return {
        "observedGeneration": generation,
        "conditions": [ready_condition(*readiness(pod, url), previous.get("conditions", []), now)],
        "sessionURL": url,
        # Printer columns show only the first match of a list JSONPath, so join the names here.
        "repos": ",".join(repo["name"] for repo in spec["repos"]),
    }
