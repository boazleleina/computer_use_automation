"""Server-side fault levers. Invisible to anything looking at the browser.

The automation must not be able to tell that a fault was arranged. That rules
out query parameters, hidden fields, cookies, and any mention in rendered HTML:
if a run could detect the lever it could learn to avoid it, and the scenario
would prove nothing.

So the levers live here, behind a blueprint the agent is never told about, and
they are armed out of band over POST. State is process-local and deliberately
crude — this is a fixture, not a component of the system under test.

    POST /_test/arm              {"fault": "slow" | "interstitial",
                                  "times": 1, "path": "/members/100045"}
    POST /_test/expire-session   force the next request to see an expired session
    POST /_test/reset            clear armed faults and restore fixture records

Each armed fault fires once per arm, consumed by the before_request hook in
app.py. Arming twice fires twice.

`path` scopes an arm to one request path. Prefer it. An unscoped arm fires on
whichever request happens to qualify next, which is fine interactively and a
liability when producing evidence that a specific step recovered — and the
interstitial fault only fires on GET, so an unscoped arm placed before a POST
waits and then goes off somewhere unrelated.
"""

from typing import Any, Final

from flask import Blueprint, jsonify, request
from flask.typing import ResponseReturnValue

from target_app import fixtures

FAULT_SLOW: Final = "slow"
FAULT_INTERSTITIAL: Final = "interstitial"
KNOWN_FAULTS: Final = (FAULT_SLOW, FAULT_INTERSTITIAL)

# How long the slow fault stalls for. Chosen to exceed a five second action
# budget, so a run meets a genuine timeout rather than a shortened one.
SLOW_FAULT_SECONDS: Final = 6.0

# One entry per queued firing, holding the path it is scoped to, or None for an
# arm that will fire on any path.
_armed: dict[str, list[str | None]] = {FAULT_SLOW: [], FAULT_INTERSTITIAL: []}
_force_expiry = False

blueprint = Blueprint("_test", __name__, url_prefix="/_test")


def arm(fault: str, times: int = 1, path: str | None = None) -> None:
    """Queue `times` firings of `fault`, optionally scoped to one request path."""
    if fault not in KNOWN_FAULTS:
        raise ValueError(f"unknown fault {fault!r}")
    _armed[fault].extend([path] * max(0, times))


def consume(fault: str, path: str) -> bool:
    """Whether `fault` should fire for this request. Removes the arm if it does.

    A scoped arm is left in place when the path does not match, so it is still
    waiting for the request it was meant for rather than being spent on another.
    """
    queue = _armed.get(fault, [])
    for index, scope in enumerate(queue):
        if scope is None or scope == path:
            queue.pop(index)
            return True
    return False


def force_expiry() -> None:
    """Make the next authenticated request behave as though the session aged out."""
    global _force_expiry
    _force_expiry = True


def consume_forced_expiry() -> bool:
    """Whether the forced expiry should fire now. Clears when it does."""
    global _force_expiry
    if not _force_expiry:
        return False
    _force_expiry = False
    return True


def reset() -> None:
    """Disarm everything and restore the fixture records."""
    global _force_expiry
    for queue in _armed.values():
        queue.clear()
    _force_expiry = False
    fixtures.reset_members()


def state() -> dict[str, Any]:
    """Current lever state. For the harness, never rendered into a page."""
    return {
        "armed": {fault: list(queue) for fault, queue in _armed.items()},
        "force_expiry": _force_expiry,
    }


@blueprint.post("/arm")
def arm_route() -> ResponseReturnValue:
    payload = request.get_json(silent=True) or {}
    fault = str(payload.get("fault", ""))
    times = int(payload.get("times", 1))
    raw_path = payload.get("path")
    path = str(raw_path) if raw_path is not None else None
    if fault not in KNOWN_FAULTS:
        return jsonify({"error": f"unknown fault {fault!r}", "known": list(KNOWN_FAULTS)}), 400
    arm(fault, times, path)
    return jsonify({"armed": fault, "times": times, "path": path, "state": state()})


@blueprint.post("/expire-session")
def expire_session_route() -> ResponseReturnValue:
    force_expiry()
    return jsonify({"expired": True, "state": state()})


@blueprint.post("/reset")
def reset_route() -> ResponseReturnValue:
    reset()
    return jsonify({"reset": True, "state": state()})
