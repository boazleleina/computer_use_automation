"""The gate: discovery produces an artifact the replay engine runs untouched.

One thread, end to end, against the real browser and the real application:

    a goal -> a discovery run -> a trajectory -> a compiled capability
           -> that capability replayed, with no compiler and no model in sight

Nothing is hand-edited between the compile and the replay. If a step had to be
fixed up in between, the compiler would not be producing artifacts, it would be
producing drafts of them, and the claim the system rests on would be false.

The model here is recorded rather than live. What is under test is the
machinery around the decisions — the loop, the policy checks, the compiler —
and that machinery behaves identically whoever made them, so putting a network
call and a billing account in front of every run of this suite would buy
nothing and cost enough that the suite stops being run.

What this therefore does not cover: whether a model can find its way through a
screen it has not seen. Only a live run answers that, and evidence/ holds one.
"""

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml
from werkzeug.serving import make_server

from cua.adapters.clocks import RealClock
from cua.adapters.playwright_surface import browser_session
from cua.adapters.recorded_model import RecordedModel
from cua.app.discover import DiscoverCapability
from cua.app.replay import ReplayCapability
from cua.domain.actions import ActionType, Effect
from cua.domain.artifact import capability_from_document, capability_to_document
from cua.domain.capability import Approval, SignalKind
from cua.domain.compiler import compile_capability
from cua.domain.errors import MalformedArtifact
from cua.domain.outcomes import Outcome
from cua.domain.policy import DeniedControl, Policy
from cua.domain.trajectory import StopReason, Trajectory
from tests.integration.test_replay_scripted import ARTIFACT, MEMBER_ID

pytest.importorskip("playwright", reason="the live surface needs a browser")

TRANSCRIPT = Path(__file__).resolve().parents[1] / "fixtures" / "discovery_transcript.jsonl"

GOAL = "Look up member 100045 and read their current savings balance and account name."
HEADLESS = os.environ.get("CUA_HEADLESS", "1") != "0"


@pytest.fixture(scope="module")
def target_app() -> Iterator[str]:
    os.environ.setdefault("TARGET_APP_USER", "tmiller")
    os.environ.setdefault("TARGET_APP_PASSWORD", "not-a-real-password")

    from target_app.app import create_app

    server = make_server("127.0.0.1", 0, create_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def policy_for(base_url: str) -> Policy:
    """The same rules discovery ran under and replay runs under.

    One Policy for both halves, not two that happen to agree. A discovery run
    permitted to go somewhere replay is not would compile a capability that
    cannot execute.
    """
    return Policy(
        allowed_origins=(base_url,),
        allowed_routes=("/login", "/search", "/members/{member_id}"),
        allowed_actions=frozenset({ActionType.CLICK, ActionType.TYPE, ActionType.READ}),
        denied_controls=(DeniedControl(role="link", name="Sign Off"),),
    )


def sign_on(surface: object, base_url: str) -> None:
    """A person signs on first. Neither half of the system holds a credential."""
    page = surface.page  # type: ignore[attr-defined]
    page.goto(f"{base_url}/login")
    page.fill("#ctl00_cph_txtUser", os.environ["TARGET_APP_USER"])
    page.fill("#ctl00_cph_txtPass", os.environ["TARGET_APP_PASSWORD"])
    page.click("#ctl00_cph_btnSignOn")
    page.wait_for_url(f"{base_url}/search")


@pytest.fixture(scope="module")
def discovered(target_app: str) -> Trajectory:
    """One discovery run against the live application."""
    with browser_session(target_app, headless=HEADLESS) as surface:
        sign_on(surface, target_app)
        engine = DiscoverCapability(
            surface=surface,
            model=RecordedModel.from_transcript(TRANSCRIPT),
            policy=policy_for(target_app),
            clock=RealClock(),
        )
        return engine.run(GOAL, run_id="discovery")


# ---- the run ----------------------------------------------------------------


def test_the_run_reached_its_goal(discovered: Trajectory):
    assert discovered.stopped_because is StopReason.GOAL_REACHED
    assert discovered.succeeded


def test_the_trajectory_holds_only_what_was_permitted(discovered: Trajectory):
    """Five actions, each of which passed policy before it happened.

    The complete proposal is not among them: it ended the run rather than
    doing anything to the application.
    """
    assert len(discovered.steps) == 5
    assert [step.proposal.action_type for step in discovered.steps] == [
        ActionType.TYPE,
        ActionType.CLICK,
        ActionType.CLICK,
        ActionType.READ,
        ActionType.READ,
    ]


def test_every_step_carries_a_rationale(discovered: Trajectory):
    """One sentence per action is what makes the log readable months later."""
    assert all(step.proposal.rationale.strip() for step in discovered.steps)


def test_the_run_read_the_values_the_goal_asked_for(discovered: Trajectory):
    assert [step.read_value for step in discovered.reads] == ["4820.55", "Test Member One"]


# ---- the compile ------------------------------------------------------------


@pytest.fixture(scope="module")
def compiled(discovered: Trajectory):
    return compile_capability(
        discovered,
        name="lookup_member_balance",
        version="1.0.0",
        inputs={"member_id": MEMBER_ID},
        app="riverside_cu_backoffice",
        release="4.2.11",
    )


def test_the_literal_became_a_parameter(compiled):
    """The point of compiling rather than saving the recording.

    100045 appears nowhere in the artifact: not in the value that gets typed,
    and not in the name of the result row that gets clicked, which is named
    after the member number itself.
    """
    document = _as_document(compiled)

    assert MEMBER_ID not in document
    assert "{{ inputs.member_id }}" in document


def test_a_value_cell_is_targeted_through_its_row_header(compiled):
    """The cell is named by its own contents, so role and name cannot find it
    without already knowing the answer."""
    read_steps = [step for step in compiled.steps if step.action_type is ActionType.READ]

    assert len(read_steps) == 2
    for step in read_steps:
        assert [signal.kind for signal in step.target.signals] == [SignalKind.ANCHOR]
    assert {step.target.signals[0].name for step in read_steps} == {
        "Savings Balance",
        "Account Name",
    }


def test_the_outputs_are_named_after_the_screen(compiled):
    assert [output.name for output in compiled.contract.outputs] == [
        "savings_balance",
        "account_name",
    ]


def test_a_discovered_capability_is_a_draft(compiled):
    """A capability a model wrote must not run unattended until a person has
    read it. Defaulting the other way would make review optional in practice."""
    assert compiled.contract.approval is Approval.DRAFT
    assert not compiled.contract.approved


def test_the_effect_is_not_guessed_optimistically(compiled):
    """Something was typed and clicked, so this is not read_only.

    Wrong in this direction costs a review. Wrong in the other direction lets
    may_run_unattended wave through a capability that changes data.
    """
    assert compiled.contract.effect is Effect.MUTATING


def test_a_run_that_stopped_cannot_be_compiled(discovered: Trajectory):
    """Most of a task is the worst outcome available: it leaves the
    application half way through a flow and tells the caller nothing."""
    from dataclasses import replace

    stopped = replace(discovered, stopped_because=StopReason.MAX_STEPS)

    with pytest.raises(MalformedArtifact):
        compile_capability(stopped, name="x", version="1.0.0", inputs={})


# ---- the gate ---------------------------------------------------------------


def test_a_discovered_capability_will_not_run_unattended(compiled, target_app: str):
    """The safety gate fires before the first step, not after the last.

    A model wrote this flow twenty seconds ago and nobody has read it. The
    refusal is the system working: discovery moves the decision to review time,
    and a capability that ran before anybody reviewed it would have moved
    nothing at all.
    """
    with browser_session(target_app, headless=HEADLESS) as surface:
        sign_on(surface, target_app)
        engine = ReplayCapability(
            surface=surface, policy=policy_for(target_app), clock=RealClock()
        )
        result = engine.run(compiled, {"member_id": MEMBER_ID}, run_id="before_review")

    assert result.outcome is Outcome.HARD_FAILURE
    assert result.observed == "approval_required"


def test_the_compiled_artifact_replays_once_approved(compiled, target_app: str):
    """The gate. Approval is the only thing a person supplies.

    Not one step, target, checkpoint or output is edited between compiling and
    replaying — the review flips an approval flag and nothing else, which is
    what makes this a compiler rather than a generator of drafts. The engine
    running it is the one Phase 3 built, with no knowledge that a compiler
    exists.
    """
    from dataclasses import replace

    reviewed = replace(
        compiled, contract=replace(compiled.contract, approval=Approval.APPROVED)
    )
    assert reviewed.steps == compiled.steps
    assert reviewed.success == compiled.success

    with browser_session(target_app, headless=HEADLESS) as surface:
        sign_on(surface, target_app)
        engine = ReplayCapability(
            surface=surface, policy=policy_for(target_app), clock=RealClock()
        )
        result = engine.run(reviewed, {"member_id": MEMBER_ID}, run_id="replay_of_compiled")

    assert result.outcome is Outcome.SUCCESS
    assert result.outputs == {
        "savings_balance": "4820.55",
        "account_name": "Test Member One",
    }


def test_it_is_structurally_the_handwritten_artifact(compiled):
    """The two were written independently and describe the same flow.

    Step ids and wording differ — one was named by a person and one by the
    compiler — so this compares the shape: the same actions in the same order,
    finding controls by the same kinds of signal, returning the same outputs.
    """
    with open(ARTIFACT, encoding="utf-8") as handle:
        handwritten = capability_from_document(yaml.safe_load(handle))

    assert [step.action_type for step in compiled.steps] == [
        step.action_type for step in handwritten.steps
    ]
    assert [_strongest(step) for step in compiled.steps] == [
        _strongest(step) for step in handwritten.steps
    ]
    assert [output.name for output in compiled.contract.outputs] == [
        output.name for output in handwritten.contract.outputs
    ]
    assert [step.reads_into for step in compiled.steps] == [
        step.reads_into for step in handwritten.steps
    ]


def _strongest(step) -> SignalKind | None:
    return step.target.signals[0].kind if step.target else None


def _as_document(capability) -> str:
    """The whole artifact as text, for the leak assertion.

    Every field, not a chosen few. A leak check that inspects the parts I
    thought to list proves only that I thought of the right parts, and the
    member number turning up in a page title inside a condition detail is
    exactly the kind of place nobody lists.
    """
    return json.dumps(asdict(capability), default=str)


def test_the_compiled_artifact_survives_being_written_and_read_back(compiled, tmp_path):
    """The gate above proves the object replays. This proves the file does.

    They are not the same claim, and the difference cost a shipped artifact:
    serialised with asdict, the document used the domain's own field names and
    the loader — which reads the author's vocabulary — refused it with "missing
    required key 'action'". Every test handed the object straight to the engine,
    so nothing noticed that the thing on disk was unloadable.
    """
    path = tmp_path / "capability.yaml"
    path.write_text(
        yaml.safe_dump(capability_to_document(compiled), sort_keys=False), encoding="utf-8"
    )

    reloaded = capability_from_document(yaml.safe_load(path.read_text(encoding="utf-8")))

    assert reloaded.contract.name == compiled.contract.name
    assert reloaded.contract.version == compiled.contract.version
    assert reloaded.contract.effect is compiled.contract.effect
    assert [s.id for s in reloaded.steps] == [s.id for s in compiled.steps]
    assert [s.action_type for s in reloaded.steps] == [s.action_type for s in compiled.steps]
    assert [s.value for s in reloaded.steps] == [s.value for s in compiled.steps]
    assert [s.reads_into for s in reloaded.steps] == [s.reads_into for s in compiled.steps]
    assert [o.name for o in reloaded.contract.outputs] == [
        o.name for o in compiled.contract.outputs
    ]
    assert [c.name for c in reloaded.conditions] == [c.name for c in compiled.conditions]
    assert reloaded.success == compiled.success

    # The targets are the part that actually finds a control, so they are
    # compared signal by signal rather than by counting them.
    for fresh, original in zip(reloaded.steps, compiled.steps, strict=True):
        if original.target is None or fresh.target is None:
            assert fresh.target is None and original.target is None
            continue
        assert [(s.kind, s.role, s.name, s.relation) for s in fresh.target.signals] == [
            (s.kind, s.role, s.name, s.relation) for s in original.target.signals
        ]


def test_the_artifact_read_back_off_disk_replays(compiled, target_app: str, tmp_path):
    """The whole thread, through a file: compile, write, load, approve, run."""
    from dataclasses import replace

    path = tmp_path / "capability.yaml"
    path.write_text(
        yaml.safe_dump(capability_to_document(compiled), sort_keys=False), encoding="utf-8"
    )
    loaded = capability_from_document(yaml.safe_load(path.read_text(encoding="utf-8")))
    approved = replace(loaded, contract=replace(loaded.contract, approval=Approval.APPROVED))

    with browser_session(target_app, headless=HEADLESS) as surface:
        sign_on(surface, target_app)
        engine = ReplayCapability(
            surface=surface, policy=policy_for(target_app), clock=RealClock()
        )
        result = engine.run(approved, {"member_id": MEMBER_ID}, run_id="from_disk")

    assert result.outcome is Outcome.SUCCESS
    assert result.outputs == {
        "savings_balance": "4820.55",
        "account_name": "Test Member One",
    }
