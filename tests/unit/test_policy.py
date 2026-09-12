"""What may this run touch?

Two independent layers, both always applied. An allowlist bounds where a run may
go; a denial bounds what it may touch. They are not modes, and giving one up to
get the other is what lets a one-click hazard through.

The other half — that a refused action never reaches Surface.act — needs the
replay engine and a recording surface, and lands with them. What is covered here
is that Policy refuses, and refuses for a stated reason.
"""

from datetime import UTC, datetime

import pytest

from cua.domain.actions import ActionType, Effect
from cua.domain.observation import Node, NodeRef, Observation, Rect
from cua.domain.policy import Allowed, Denied, DeniedControl, Policy, PolicyRule

ROUTES = (
    "/login",
    "/search",
    "/members/{member_id}",
    "/members/{member_id}/subaccount/new",
)

POLICY = Policy(
    allowed_origins=("http://127.0.0.1:5000",),
    allowed_routes=ROUTES,
    allowed_actions=frozenset({ActionType.CLICK, ActionType.TYPE, ActionType.READ}),
    denied_controls=(DeniedControl(role="link", name="Sign Off"),),
)

# Same policy with the landmine list emptied, to show the route layer stands on
# its own rather than being decoration next to denied_controls.
POLICY_WITHOUT_DENIALS = Policy(
    allowed_origins=POLICY.allowed_origins,
    allowed_routes=POLICY.allowed_routes,
    allowed_actions=POLICY.allowed_actions,
    denied_controls=(),
)

# Navigation is not in the allowlist above, so the origin and route rules would
# never be reached. A separate policy rather than a per-call override: the
# allowlist is a property of the run, not an argument to a single decision.
NAVIGATING_POLICY = Policy(
    allowed_origins=POLICY.allowed_origins,
    allowed_routes=POLICY.allowed_routes,
    allowed_actions=frozenset({ActionType.NAVIGATE}),
    denied_controls=POLICY.denied_controls,
)


def node(role: str, name: str | None, destination: str | None = None) -> Node:
    return Node(
        ref=NodeRef(observation_id="obs_test", value="main:0"),
        role=role,
        name=name,
        text=None,
        frame_id="main",
        bounds=Rect(x=0.0, y=0.0, width=10.0, height=10.0),
        enabled=True,
        visible=True,
        destination=destination,
    )


def observation(*nodes: Node, url_pattern: str = "/search") -> Observation:
    return Observation(
        observation_id="obs_test",
        nodes=nodes,
        url_pattern=url_pattern,
        page_title="Test",
        captured_at=datetime.now(UTC),
    )


def test_an_allowlisted_action_on_an_ordinary_control_is_allowed():
    verdict = POLICY.evaluate(ActionType.CLICK, node=node("button", "Find"))
    assert isinstance(verdict, Allowed)


@pytest.mark.parametrize("action", [ActionType.SELECT, ActionType.NAVIGATE])
def test_an_action_outside_the_allowlist_is_refused(action):
    """The allowlist is closed. An action nobody considered is refused, not permitted."""
    verdict = POLICY.evaluate(action, node=node("button", "Find"), route="/search")
    assert isinstance(verdict, Denied)
    assert verdict.rule is PolicyRule.ACTION_NOT_ALLOWED


def test_a_denied_control_is_refused_even_though_the_action_is_allowed():
    """Sign Off is a link, and clicking links is allowed.

    The action allowlist has nothing to say here, which is the whole point of
    keeping the two layers separate.
    """
    verdict = POLICY.evaluate(ActionType.CLICK, node=node("link", "Sign Off", "/logout"))
    assert isinstance(verdict, Denied)
    assert verdict.rule is PolicyRule.CONTROL_DENIED


def test_a_link_leaving_the_allowed_routes_is_refused_before_it_is_followed():
    """The check happens on the control, not on the destination after arrival.

    A route allowlist evaluated after navigation cannot undo an irreversible
    side effect. Node.destination exists so the refusal can happen while the
    click is still a proposal.
    """
    verdict = POLICY_WITHOUT_DENIALS.evaluate(
        ActionType.CLICK, node=node("link", "Sign Off", "/logout")
    )
    assert isinstance(verdict, Denied)
    assert verdict.rule is PolicyRule.ROUTE_NOT_ALLOWED


def test_sign_off_is_refused_by_either_layer_alone():
    """Defence in depth, stated as a test.

    With both layers the control rule fires first because it is the more
    specific statement. With the landmine list emptied the route rule still
    refuses. Neither layer is load bearing on its own.
    """
    with_both = POLICY.evaluate(ActionType.CLICK, node=node("link", "Sign Off", "/logout"))
    with_routes_only = POLICY_WITHOUT_DENIALS.evaluate(
        ActionType.CLICK, node=node("link", "Sign Off", "/logout")
    )

    assert isinstance(with_both, Denied)
    assert isinstance(with_routes_only, Denied)
    assert with_both.rule is PolicyRule.CONTROL_DENIED
    assert with_routes_only.rule is PolicyRule.ROUTE_NOT_ALLOWED


def test_a_link_staying_inside_the_allowed_routes_is_allowed():
    verdict = POLICY.evaluate(
        ActionType.CLICK,
        node=node("link", "Open Sub-account", "/members/{member_id}/subaccount/new"),
    )
    assert isinstance(verdict, Allowed)


def test_a_control_with_no_knowable_destination_is_not_refused_by_the_route_layer():
    """The documented gap.

    A submit button has no href, so the route layer is blind to where it leads.
    Nothing here is wrong; it is why denied_controls has to exist at all, and
    why removing it in favour of routes alone would be a downgrade.
    """
    verdict = POLICY_WITHOUT_DENIALS.evaluate(ActionType.CLICK, node=node("button", "Sign On"))
    assert isinstance(verdict, Allowed)


def test_navigating_off_the_allowed_origin_is_refused():
    """A misread link must not walk the run onto a real site with a live session."""
    verdict = NAVIGATING_POLICY.evaluate(
        ActionType.NAVIGATE, route="https://example.invalid/members/1"
    )
    assert isinstance(verdict, Denied)
    assert verdict.rule is PolicyRule.ORIGIN_NOT_ALLOWED


def test_navigating_with_no_route_at_all_is_refused():
    """Fails closed.

    Navigation with nowhere to go skips every guard evaluate has: no control to
    match against the denied list, no destination, no route to compare. A step
    whose route template went missing would otherwise pass the one layer whose
    job is bounding where a run may go.
    """
    verdict = NAVIGATING_POLICY.evaluate(ActionType.NAVIGATE, route=None)

    assert isinstance(verdict, Denied)
    assert verdict.rule is PolicyRule.ROUTE_NOT_ALLOWED
    assert "nothing to check it against" in verdict.reason


def test_an_action_that_is_not_navigation_needs_no_route():
    """The rule is about navigation, not about caution generally. A click is
    checked through the control it names."""
    verdict = POLICY.evaluate(ActionType.CLICK, node=node("button", "Find"), route=None)

    assert isinstance(verdict, Allowed)


def test_navigating_to_an_undeclared_route_on_the_right_origin_is_refused():
    verdict = NAVIGATING_POLICY.evaluate(ActionType.NAVIGATE, route="/admin/users")
    assert isinstance(verdict, Denied)
    assert verdict.rule is PolicyRule.ROUTE_NOT_ALLOWED


def test_every_denial_states_a_reason():
    """A denial with no stated cause cannot be written into evidence as an
    account of why a run stopped."""
    denials = [
        POLICY.evaluate(ActionType.SELECT, node=node("button", "Find")),
        POLICY.evaluate(ActionType.CLICK, node=node("link", "Sign Off", "/logout")),
        POLICY_WITHOUT_DENIALS.evaluate(ActionType.CLICK, node=node("link", "Away", "/logout")),
    ]
    for verdict in denials:
        assert isinstance(verdict, Denied)
        assert verdict.reason
        assert verdict.rule.value in verdict.reason or verdict.reason.strip()


def test_allowed_carries_nothing_to_misuse():
    """Symmetry with Resolved/Ambiguous: a verdict is the answer, not a hint."""
    verdict = POLICY.evaluate(ActionType.READ, node=node("cell", "4820.55"))
    assert isinstance(verdict, Allowed)
    assert not hasattr(verdict, "reason")


def test_a_read_only_capability_needs_no_approval_to_run_unattended():
    """The worst it can do is read the wrong number, which surfaces as a wrong
    answer rather than a wrong account."""
    assert isinstance(POLICY.may_run_unattended(Effect.READ_ONLY, approved=False), Allowed)


@pytest.mark.parametrize("effect", [Effect.MUTATING, Effect.IRREVERSIBLE])
def test_a_writing_capability_is_refused_until_it_has_been_approved(effect):
    """Writing a procedure down is only worth anything if somebody signs it off
    before it runs thousands of times."""
    verdict = POLICY.may_run_unattended(effect, approved=False)

    assert isinstance(verdict, Denied)
    assert verdict.rule is PolicyRule.APPROVAL_REQUIRED
    assert effect.value in verdict.reason


@pytest.mark.parametrize("effect", [Effect.MUTATING, Effect.IRREVERSIBLE])
def test_an_approved_writing_capability_may_run(effect):
    assert isinstance(POLICY.may_run_unattended(effect, approved=True), Allowed)


def test_approval_is_asked_once_per_run_not_once_per_action():
    """A different question from whether an individual action is allowed.

    Sign Off is refused whatever the capability's effect class is, and a
    mutating capability needs approval even when every action it takes is on
    the allowlist.
    """
    action = POLICY.evaluate(ActionType.CLICK, node=node("button", "Continue"))
    unattended = POLICY.may_run_unattended(Effect.MUTATING, approved=False)

    assert isinstance(action, Allowed)
    assert isinstance(unattended, Denied)


def test_the_sign_off_control_in_the_real_fixture_is_refused(search_page):
    """Against the captured page rather than a hand-built one.

    search_page carries Sign Off with destination /logout, exactly as the live
    application renders it.
    """
    sign_off = next(n for n in search_page.nodes if n.name == "Sign Off")
    assert sign_off.destination == "/logout"

    verdict = POLICY.evaluate(ActionType.CLICK, node=sign_off)
    assert isinstance(verdict, Denied)


def test_no_action_is_permitted_on_a_control_that_holds_a_credential():
    """The hole a live discovery run walked straight through.

    Asked to look up a member from a signed out session, the model did the
    sensible thing: it found the sign on form, guessed a user id and a password,
    and typed them. Policy allowed it — type was an allowed action, the field
    was on an allowed route and was not in the denied list, and nothing in a
    Node said it was a password box.

    The claim that this system never touches credentials was only ever true of
    capabilities, where Contract refuses a secret input. Discovery is not a
    capability.
    """
    password = Node(
        ref=NodeRef(observation_id="obs_1", value="main:3"),
        role="textbox",
        name="Password",
        text=None,
        frame_id="main",
        bounds=Rect(x=0.0, y=0.0, width=10.0, height=10.0),
        enabled=True,
        visible=True,
        secret=True,
    )
    policy = Policy(
        allowed_origins=("http://127.0.0.1:5000",),
        allowed_routes=("/login",),
        allowed_actions=frozenset(ActionType),
        denied_controls=(),
    )

    # Every action that addresses a control, not only type: reading one returns
    # whatever is in it, and clicking one is at best pointless. Navigate is not
    # in the list because it addresses a route and is refused before a control
    # is looked at.
    for action in (ActionType.TYPE, ActionType.CLICK, ActionType.SELECT, ActionType.READ):
        verdict = policy.evaluate(action, password)
        assert isinstance(verdict, Denied), action
        assert verdict.rule is PolicyRule.SECRET_CONTROL, action


def test_an_ordinary_field_beside_a_password_is_unaffected():
    """The refusal is a property of the control, not of the screen it is on."""
    user_id = Node(
        ref=NodeRef(observation_id="obs_1", value="main:2"),
        role="textbox",
        name="User Id",
        text=None,
        frame_id="main",
        bounds=Rect(x=0.0, y=0.0, width=10.0, height=10.0),
        enabled=True,
        visible=True,
    )
    policy = Policy(
        allowed_origins=("http://127.0.0.1:5000",),
        allowed_routes=("/login",),
        allowed_actions=frozenset({ActionType.TYPE}),
        denied_controls=(),
    )

    assert isinstance(policy.evaluate(ActionType.TYPE, user_id), Allowed)
