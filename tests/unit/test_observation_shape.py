"""How a screen is reduced to an Observation.

Two different reductions live here and the difference between them is a
security boundary, so they get their own tests rather than being covered
incidentally by a surface that happens to call them.
"""

from cua.adapters.observation_shape import destination_pattern, route_pattern
from cua.domain.actions import ActionType
from cua.domain.observation import Node, NodeRef, Rect
from cua.domain.policy import Allowed, Denied, Policy, PolicyRule

ORIGIN = "http://127.0.0.1:5000"


def link(destination: str) -> Node:
    return Node(
        ref=NodeRef(observation_id="obs_1", value="main:0"),
        role="link",
        name="Continue",
        text=None,
        frame_id="main",
        bounds=Rect(x=0.0, y=0.0, width=10.0, height=10.0),
        enabled=True,
        visible=True,
        destination=destination,
    )


def policy() -> Policy:
    return Policy(
        allowed_origins=(ORIGIN,),
        allowed_routes=("/login", "/search", "/members/{member_id}"),
        allowed_actions=frozenset({ActionType.CLICK}),
        denied_controls=(),
    )


def test_a_route_carries_no_identifier():
    """A route is written to evidence, so a member number in one is a member
    number in a file. It is also what lets one capability serve every member."""
    assert route_pattern(f"{ORIGIN}/members/100045") == "/members/{member_id}"
    assert route_pattern("/members/100045?tab=savings") == "/members/{member_id}"


def test_a_destination_keeps_its_origin():
    """Unlike a route. Policy checks a destination against the origin
    allowlist, and one it never sees is one it cannot refuse."""
    assert destination_pattern("https://elsewhere.example/search") == (
        "https://elsewhere.example/search"
    )
    assert destination_pattern("/search") == "/search"


def test_a_destination_still_drops_identifiers():
    """Keeping the origin must not smuggle a member number back in."""
    assert destination_pattern(f"{ORIGIN}/members/100045") == f"{ORIGIN}/members/{{member_id}}"


def test_an_off_origin_link_is_refused():
    """The reason the two functions are not one.

    A cross origin link reduced to its path arrives at the policy looking like
    a local route, and /search is allowed, so the click is permitted and the
    run leaves the application. Reducing it with destination_pattern keeps the
    origin in front of the rule that exists to catch it.
    """
    verdict = policy().evaluate(
        ActionType.CLICK,
        link(destination_pattern("https://elsewhere.example/search")),
        route=None,
    )

    assert isinstance(verdict, Denied)
    assert verdict.rule is PolicyRule.ORIGIN_NOT_ALLOWED


def test_an_on_origin_link_is_still_allowed():
    """The fix must not deny the ordinary case."""
    verdict = policy().evaluate(
        ActionType.CLICK,
        link(destination_pattern(f"{ORIGIN}/members/100045")),
        route=None,
    )

    assert isinstance(verdict, Allowed)
