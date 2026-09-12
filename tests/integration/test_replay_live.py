"""The same artifact, against a real browser and a real application.

Nothing here is written for the browser. The capability, the conditions and the
policy are the ones the scripted tests use, loaded from the same file, and the
only thing that changes is which implementation of the port is wired in.

That is the claim the whole architecture rests on, so it is worth being precise
about what would falsify it: if a run needed a different artifact, a different
condition, or a special case for the live surface, the seam would not be real
and the scripted tests would have been testing a fiction.
"""

import json
import os
import threading
import urllib.request
from collections.abc import Iterator

import pytest
import yaml
from werkzeug.serving import make_server

from cua.adapters.clocks import RealClock
from cua.adapters.playwright_surface import PlaywrightSurface, browser_session
from cua.app.replay import ReplayCapability
from cua.domain.actions import ActionType
from cua.domain.artifact import capability_from_document
from cua.domain.capability import Capability
from cua.domain.outcomes import Outcome, Result
from cua.domain.policy import DeniedControl, Policy
from tests.integration.test_replay_scripted import ARTIFACT, MEMBER_ID

pytest.importorskip("playwright", reason="the live surface needs a browser")

# Watch the run in a real window with:  CUA_HEADLESS=0 CUA_SLOW_MO=400 pytest ...
# Only the pace changes; the run takes the same path either way.
HEADLESS = os.environ.get("CUA_HEADLESS", "1") != "0"
SLOW_MO_MS = int(os.environ.get("CUA_SLOW_MO", "0"))


@pytest.fixture(scope="module")
def target_app() -> Iterator[str]:
    """The legacy application, started for these tests and torn down after.

    In process and on a free port, so the suite does not depend on somebody
    having left a server running, and two runs cannot collide.

    The server picks the port itself rather than being told one a probe socket
    found and released. Between releasing the probe and binding the server,
    anything on the machine may take it.
    """
    os.environ.setdefault("TARGET_APP_USER", "tmiller")
    # setdefault, so a real .env still wins. The literal is only a floor, and
    # it guards a server that lives for the length of this module on a port the
    # OS picked, so it is named to read as inert to anyone grepping for one.
    os.environ.setdefault("TARGET_APP_PASSWORD", "not-a-real-password")

    from target_app.app import create_app

    server = make_server("127.0.0.1", 0, create_app(), threaded=True)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def capability() -> Capability:
    with open(ARTIFACT, encoding="utf-8") as handle:
        return capability_from_document(yaml.safe_load(handle))


def policy_for(base_url: str) -> Policy:
    """The same rules as the scripted tests, pointed at this server."""
    return Policy(
        allowed_origins=(base_url,),
        allowed_routes=("/login", "/search", "/members/{member_id}"),
        allowed_actions=frozenset({ActionType.CLICK, ActionType.TYPE, ActionType.READ}),
        denied_controls=(DeniedControl(role="link", name="Sign Off"),),
    )


def sign_on(surface: PlaywrightSurface, base_url: str) -> None:
    """A person signs on. The automation never holds the credential.

    Done here with the driver directly rather than through a capability,
    because a capability cannot declare a secret input: Contract refuses to be
    constructed with one.
    """
    surface.page.goto(f"{base_url}/login")
    surface.page.fill("#ctl00_cph_txtUser", os.environ["TARGET_APP_USER"])
    surface.page.fill("#ctl00_cph_txtPass", os.environ["TARGET_APP_PASSWORD"])
    surface.page.click("#ctl00_cph_btnSignOn")
    surface.page.wait_for_url(f"{base_url}/search")


def arm(base_url: str, lever: str, payload: dict[str, object] | None = None) -> None:
    """Pull a server side fault lever. Nothing in the browser reveals it."""
    request = urllib.request.Request(
        f"{base_url}/_test/{lever}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def replay(
    capability: Capability, base_url: str, member_id: str = MEMBER_ID, before: object = None
) -> Result:
    """One run against the live application, signed on first."""
    with browser_session(base_url, headless=HEADLESS, slow_mo_ms=SLOW_MO_MS) as surface:
        sign_on(surface, base_url)
        if callable(before):
            before()
        engine = ReplayCapability(
            surface=surface, policy=policy_for(base_url), clock=RealClock()
        )
        return engine.run(capability, {"member_id": member_id}, run_id="live")


# ----------------------------------------------------------- the five, again


def test_success(capability, target_app):
    """The unmodified artifact reads a real balance out of a real browser."""
    arm(target_app, "reset")
    result = replay(capability, target_app)

    assert result.outcome is Outcome.SUCCESS
    assert result.outputs == {
        "savings_balance": "4820.55",
        "account_name": "Test Member One",
    }


def test_business_outcome(capability, target_app):
    arm(target_app, "reset")
    result = replay(capability, target_app, member_id="100099")

    assert result.outcome is Outcome.BUSINESS_OUTCOME
    assert result.condition_name == "member_not_found"


def test_hard_failure(capability, target_app):
    arm(target_app, "reset")
    result = replay(capability, target_app, member_id="100047")

    assert result.outcome is Outcome.HARD_FAILURE
    assert result.condition_name == "access_restricted"


def test_auth_intervention(capability, target_app):
    """The lever is pulled after sign on, so the session dies mid run."""
    arm(target_app, "reset")
    result = replay(
        capability, target_app, before=lambda: arm(target_app, "expire-session")
    )

    assert result.outcome is Outcome.INTERVENTION_REQUIRED
    assert result.condition_name == "session_expired"


def test_recoverable(capability, target_app):
    """A maintenance notice appears on the member page and is dismissed."""
    arm(target_app, "reset")
    result = replay(
        capability,
        target_app,
        before=lambda: arm(
            target_app,
            "arm",
            {"fault": "interstitial", "times": 1, "path": f"/members/{MEMBER_ID}"},
        ),
    )

    assert result.outcome is Outcome.SUCCESS
    assert result.outputs["savings_balance"] == "4820.55"


# ------------------------------------------------------------- the seam itself


def test_the_live_surface_sees_what_the_fixtures_recorded(target_app):
    """The captured screens describe the application, not a convenient fiction.

    If these drifted apart, the scripted suite would be green against behaviour
    the browser does not produce, and every conclusion drawn from it would be
    worth nothing.
    """
    from tests.conftest import load_observation

    # Whatever the previous test armed is still armed. These two do not set a
    # lever themselves, which is precisely why they would inherit one.
    arm(target_app, "reset")

    with browser_session(target_app, headless=HEADLESS, slow_mo_ms=SLOW_MO_MS) as surface:
        sign_on(surface, target_app)
        live = surface.observe()

    recorded = load_observation("search_page")

    assert [(n.role, n.name, n.destination) for n in live.nodes] == [
        (n.role, n.name, n.destination) for n in recorded.nodes
    ]
    assert live.url_pattern == recorded.url_pattern
    assert live.page_title == recorded.page_title


def test_a_route_carries_no_member_number(capability, target_app):
    """Identifiers must not enter an Observation, because an Observation is
    written to evidence."""
    with browser_session(target_app, headless=HEADLESS, slow_mo_ms=SLOW_MO_MS) as surface:
        sign_on(surface, target_app)
        surface.page.goto(f"{target_app}/members/{MEMBER_ID}")
        live = surface.observe()

    assert live.url_pattern == "/members/{member_id}"
    assert MEMBER_ID not in live.url_pattern
