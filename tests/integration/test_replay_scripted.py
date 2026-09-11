"""The handwritten artifact, run end to end against captured screens.

Five endings, one artifact, no browser and no model. Every screen in every
script was dumped from the running application, so a run that behaves here is
behaving against what the browser actually reports.

A script is the sequence of screens the application goes through. It says
nothing about how many times the engine looks at each one, because looking does
not change anything.
"""

from collections.abc import Sequence

import pytest
import yaml

from cua.adapters.clocks import FakeClock
from cua.adapters.scripted_surface import ScriptedSurface
from cua.app.replay import ReplayCapability
from cua.domain.actions import ActionType, Effect
from cua.domain.artifact import capability_from_document
from cua.domain.capability import Capability, SignalKind
from cua.domain.errors import MalformedArtifact
from cua.domain.observation import Observation
from cua.domain.outcomes import Outcome, Result
from cua.domain.policy import DeniedControl, Policy
from cua.domain.run import RECOVERIES_PER_STEP

ARTIFACT = "tests/fixtures/member_lookup.handwritten.yaml"
MEMBER_ID = "100045"

POLICY = Policy(
    allowed_origins=("http://127.0.0.1:5000",),
    allowed_routes=("/login", "/search", "/members/{member_id}"),
    allowed_actions=frozenset({ActionType.CLICK, ActionType.TYPE, ActionType.READ}),
    denied_controls=(DeniedControl(role="link", name="Sign Off"),),
)


@pytest.fixture
def capability() -> Capability:
    with open(ARTIFACT, encoding="utf-8") as handle:
        return capability_from_document(yaml.safe_load(handle))


def replay_against(
    screens: Sequence[Observation],
    policy: Policy = POLICY,
    clock: FakeClock | None = None,
) -> tuple[ReplayCapability, ScriptedSurface]:
    surface = ScriptedSurface(screens)
    engine = ReplayCapability(surface=surface, policy=policy, clock=clock or FakeClock())
    return engine, surface


def run(
    capability: Capability,
    screens: Sequence[Observation],
    member_id: str = MEMBER_ID,
    policy: Policy = POLICY,
) -> tuple[Result, ScriptedSurface]:
    engine, surface = replay_against(screens, policy=policy)
    result = engine.run(capability, {"member_id": member_id}, run_id="run_1")
    return result, surface


# ---------------------------------------------------------------- the artifact


def test_the_handwritten_artifact_parses(capability):
    assert capability.contract.name == "lookup_member_balance"
    assert capability.contract.version == "1.0.0"
    assert [s.id for s in capability.steps] == [
        "enter_member_number",
        "submit_lookup",
        "read_savings_balance",
        "read_account_name",
    ]


# ------------------------------------------------------------------- the five


def test_success(capability, search_page, search_page_filled, member_detail):
    """The member exists and the balance comes back."""
    result, surface = run(capability, [search_page, search_page_filled, member_detail])

    assert result.outcome is Outcome.SUCCESS
    assert result.ok
    assert result.outputs == {
        "savings_balance": "4820.55",
        "account_name": "Test Member One",
    }
    assert result.resolved_via is SignalKind.ANCHOR
    assert [call.action_type for call in surface.acted] == [ActionType.TYPE, ActionType.CLICK]


def test_business_outcome(capability, search_page, search_page_filled_unknown, not_found):
    """No such member. The run worked; this is the answer.

    The step that submitted the search was hoping for a member page and did not
    get one. Reporting that as a checkpoint failure would turn a true answer
    into a crash, so what the application says about the screen wins.
    """
    result, _ = run(
        capability,
        [search_page, search_page_filled_unknown, not_found],
        member_id="100099",
    )

    assert result.outcome is Outcome.BUSINESS_OUTCOME
    assert result.condition_name == "member_not_found"
    assert not result.ok
    assert result.outcome not in {Outcome.HARD_FAILURE, Outcome.RECOVERABLE}


def test_hard_failure(capability, search_page, search_page_filled, member_denied):
    """The operator may not view this membership. Not retryable, not an answer."""
    result, _ = run(capability, [search_page, search_page_filled, member_denied])

    assert result.outcome is Outcome.HARD_FAILURE
    assert result.condition_name == "access_restricted"
    assert "SEC-0042" not in (result.detail or "")


def test_auth_intervention(capability, search_page, search_page_filled, session_expired):
    """The session aged out. A person is needed, and no credential is typed.

    The expired page is served at the route of the member page it replaced, so
    the route alone would have read as success. Severity decides it.
    """
    result, surface = run(capability, [search_page, search_page_filled, session_expired])

    assert result.outcome is Outcome.INTERVENTION_REQUIRED
    assert result.condition_name == "session_expired"

    typed = [c.value for c in surface.acted if c.action_type is ActionType.TYPE]
    assert typed == [MEMBER_ID]  # the member number, and nothing else


def test_recoverable(capability, search_page, search_page_filled, interstitial, member_detail):
    """A maintenance notice is in the way. Dismiss it and carry on."""
    result, surface = run(
        capability, [search_page, search_page_filled, interstitial, member_detail]
    )

    assert result.outcome is Outcome.SUCCESS
    assert result.outputs["savings_balance"] == "4820.55"

    clicks = [c for c in surface.acted if c.action_type is ActionType.CLICK]
    assert len(clicks) == 2  # Find, then Continue on the notice


# ------------------------------------------------------- bounds and refusals


def test_an_interstitial_that_never_clears_asks_for_a_person(
    capability, search_page, search_page_filled, interstitial
):
    """Waiting has demonstrably stopped working, so waiting again will not help."""
    screens = [search_page, search_page_filled] + [interstitial] * 6
    result, surface = run(capability, screens)

    assert result.outcome is Outcome.INTERVENTION_REQUIRED
    dismissals = [c for c in surface.acted if c.action_type is ActionType.CLICK]
    assert len(dismissals) <= RECOVERIES_PER_STEP + 1


def test_an_action_outside_the_allowlist_never_reaches_the_surface(
    capability, search_page, search_page_filled, member_detail
):
    """The assertion that needs a surface to make: nothing was done.

    A refusal recorded after the fact is an account of something that already
    happened. With TYPE off the allowlist the first step is refused, and the
    proof is that the surface was never asked to do anything at all.
    """
    read_only = Policy(
        allowed_origins=POLICY.allowed_origins,
        allowed_routes=POLICY.allowed_routes,
        allowed_actions=frozenset({ActionType.READ}),
        denied_controls=POLICY.denied_controls,
    )
    result, surface = run(
        capability, [search_page, search_page_filled, member_detail], policy=read_only
    )

    assert result.outcome is Outcome.HARD_FAILURE
    assert result.step_id == "enter_member_number"
    assert surface.acted == []


def test_inputs_are_checked_against_the_contract_before_anything_runs(
    capability, search_page, search_page_filled, member_detail
):
    """The declared pattern is six digits. A form three screens later is a worse
    place to find that out."""
    engine, surface = replay_against([search_page, search_page_filled, member_detail])

    with pytest.raises(MalformedArtifact):
        engine.run(capability, {"member_id": "not-a-number"}, run_id="run_1")

    assert surface.acted == []


def test_a_missing_required_input_is_refused(capability, search_page):
    engine, surface = replay_against([search_page])

    with pytest.raises(MalformedArtifact):
        engine.run(capability, {}, run_id="run_1")

    assert surface.acted == []


# --------------------------------------------------------------- the evidence


def test_the_run_reports_where_it_got_to_and_what_it_expected(
    capability, search_page, search_page_filled, session_expired
):
    """"The run failed" is not actionable. This is."""
    result, _ = run(capability, [search_page, search_page_filled, session_expired])

    assert result.step_id == "submit_lookup"
    assert result.detail
    assert result.capability == "lookup_member_balance"
    assert result.version == "1.0.0"


def test_no_model_is_reachable_from_the_replay_engine():
    """Structural, not a promise. There is no Model in the engine's signature."""
    import inspect

    from cua.app import replay

    source = inspect.getsource(replay)
    assert "ModelPort" not in source
    assert "cua.ports.model" not in source


def test_a_read_only_capability_needs_no_approval(capability):
    assert capability.contract.effect is Effect.READ_ONLY
    assert not capability.contract.approved
    from cua.domain.policy import Allowed

    verdict = POLICY.may_run_unattended(
        capability.contract.effect, approved=capability.contract.approved
    )
    assert isinstance(verdict, Allowed)
