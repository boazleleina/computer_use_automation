"""Who owns the run, and what happens when a person has to take over.

The state machine exists so that "automation cannot act while a human holds the
session" is a property of the type rather than a rule spread across the engine.

Nothing here re-authenticates. A run that meets an expired session escalates and
waits; the operator signs in through the browser it is already driving.
"""

import pytest

from cua.domain.errors import InvalidTransition
from cua.domain.run import (
    ESCALATIONS_PER_REASON,
    RECOVERIES_PER_STEP,
    EscalationReason,
    Owner,
    Run,
    RunState,
)


def started() -> Run:
    return Run.start(run_id="run_1", capability="lookup_member_balance", version=1)


def paused_for_auth() -> Run:
    return started().escalate(
        EscalationReason.AUTHENTICATION_REQUIRED,
        resume_checkpoint="authenticated_session",
    )


def test_a_started_run_is_running_and_owned_by_the_automation():
    run = started()
    assert run.state is RunState.RUNNING
    assert run.owner is Owner.AUTOMATION
    assert run.may_act


def test_escalating_pauses_the_run_before_anyone_takes_over():
    """Paused is not yet human control. The request has been raised; nobody has
    picked it up."""
    run = paused_for_auth()

    assert run.state is RunState.PAUSED
    assert run.owner is Owner.AUTOMATION
    assert not run.may_act
    assert run.resume_checkpoint == "authenticated_session"


def test_handing_over_transfers_ownership():
    run = paused_for_auth().hand_over()

    assert run.state is RunState.HUMAN_CONTROL
    assert run.owner is Owner.HUMAN
    assert not run.may_act


@pytest.mark.parametrize(
    "state_name",
    ["paused", "human_control", "resuming", "succeeded", "failed"],
)
def test_the_automation_may_only_act_while_running(state_name):
    """The one invariant worth stating twice. Every state that is not RUNNING
    refuses action, including the two that look harmless."""
    runs = {
        "paused": paused_for_auth(),
        "human_control": paused_for_auth().hand_over(),
        "resuming": paused_for_auth().hand_over().return_control(),
        "succeeded": started().succeed(),
        "failed": started().fail("stopped by hand"),
    }
    assert not runs[state_name].may_act


def test_returning_control_does_not_resume_the_run():
    """The human has finished, and the run still may not act.

    What is on screen after a handover is unknown: the operator may have signed
    in and navigated somewhere else entirely. Resuming is a separate step that
    happens only once the checkpoint has been verified.
    """
    run = paused_for_auth().hand_over().return_control()

    assert run.state is RunState.RESUMING
    assert run.owner is Owner.AUTOMATION
    assert not run.may_act


def test_resuming_with_the_checkpoint_holding_returns_to_the_same_step():
    """The step is retried from the top, not continued from the middle.

    Whatever was resolved before pausing is stale: the NodeRef belongs to an
    observation that no longer describes the screen. The engine re-observes,
    re-classifies and re-resolves, which is why the checkpoint verifies a
    precondition instead of asserting the step already got somewhere.
    """
    before = paused_for_auth()
    resumed = before.hand_over().return_control().resume(checkpoint_holds=True)

    assert resumed.state is RunState.RUNNING
    assert resumed.owner is Owner.AUTOMATION
    assert resumed.may_act
    assert resumed.step_index == before.step_index


def test_resuming_when_the_checkpoint_does_not_hold_fails_the_run():
    """The operator was asked to sign in and the session is still not
    authenticated. Carrying on would act on a screen nobody verified."""
    run = paused_for_auth().hand_over().return_control().resume(checkpoint_holds=False)

    assert run.state is RunState.FAILED
    assert "authenticated_session" in run.failure_reason


def test_a_second_escalation_for_the_same_reason_fails_instead():
    """Once is a session that aged out. Twice is a session length shorter than
    the flow takes, or an application invalidating the session on every step.
    Pulling the operator back for the same problem wastes their time and will
    not fix it.
    """
    run = paused_for_auth().hand_over().return_control().resume(checkpoint_holds=True)
    run = run.escalate(
        EscalationReason.AUTHENTICATION_REQUIRED,
        resume_checkpoint="authenticated_session",
    )

    assert run.state is RunState.FAILED
    assert EscalationReason.AUTHENTICATION_REQUIRED.value in run.failure_reason
    assert str(ESCALATIONS_PER_REASON) in run.failure_reason


def test_the_bound_is_counted_per_reason_not_across_the_run():
    """An expired session and an ambiguous control are different problems.
    Spending the budget on one must not silence the other.
    """
    run = paused_for_auth().hand_over().return_control().resume(checkpoint_holds=True)
    run = run.escalate(EscalationReason.AMBIGUOUS_CONTROL, resume_checkpoint="target_unique")

    assert run.state is RunState.PAUSED
    assert run.escalation_count(EscalationReason.AMBIGUOUS_CONTROL) == 1
    assert run.escalation_count(EscalationReason.AUTHENTICATION_REQUIRED) == 1


def test_escalation_counts_survive_the_round_trip():
    run = paused_for_auth()
    assert run.escalation_count(EscalationReason.AUTHENTICATION_REQUIRED) == 1

    resumed = run.hand_over().return_control().resume(checkpoint_holds=True)
    assert resumed.escalation_count(EscalationReason.AUTHENTICATION_REQUIRED) == 1


def test_advancing_moves_the_step_and_keeps_the_run_running():
    run = started().advance()
    assert run.step_index == 1
    assert run.state is RunState.RUNNING


@pytest.mark.parametrize("terminal", ["succeeded", "failed"])
def test_a_terminal_run_accepts_no_further_transitions(terminal):
    run = started().succeed() if terminal == "succeeded" else started().fail("done")

    with pytest.raises(InvalidTransition):
        run.advance()
    with pytest.raises(InvalidTransition):
        run.escalate(EscalationReason.UNKNOWN_STATE, resume_checkpoint="anything")


def test_handing_over_a_run_that_was_never_paused_is_refused():
    """Ownership only moves through an escalation, so there is always a recorded
    reason for a person holding the session."""
    with pytest.raises(InvalidTransition):
        started().hand_over()


def test_resuming_without_the_human_returning_control_is_refused():
    with pytest.raises(InvalidTransition):
        paused_for_auth().resume(checkpoint_holds=True)


def test_a_recoverable_condition_is_retried_up_to_the_bound():
    """Waiting is allowed to fail a fixed number of times, not indefinitely."""
    run = started()
    for expected in range(1, RECOVERIES_PER_STEP + 1):
        run = run.recover()
        assert run.state is RunState.RUNNING
        assert run.recovery_count() == expected


def test_exhausting_the_recovery_bound_asks_for_a_person():
    """Waiting has demonstrably stopped working. An interstitial that will not
    clear and a page that never finishes loading both look like this, and both
    need someone to look rather than another retry.
    """
    run = started()
    for _ in range(RECOVERIES_PER_STEP):
        run = run.recover()

    run = run.recover()

    assert run.state is RunState.PAUSED
    assert run.escalation_count(EscalationReason.RECOVERY_EXHAUSTED) == 1


def test_the_recovery_bound_is_counted_per_step():
    """A slow page early in a flow must not spend the budget for an interstitial
    later in the same run."""
    run = started()
    for _ in range(RECOVERIES_PER_STEP):
        run = run.recover()
    assert run.recovery_count() == RECOVERIES_PER_STEP

    run = run.advance()

    assert run.step_index == 1
    assert run.recovery_count() == 0
    assert run.recovery_count(step_index=0) == RECOVERIES_PER_STEP

    run = run.recover()
    assert run.state is RunState.RUNNING


def test_recovery_is_refused_while_a_person_holds_the_session():
    with pytest.raises(InvalidTransition):
        paused_for_auth().hand_over().recover()


def test_transitions_do_not_mutate_the_run_they_came_from():
    """Run history is a sequence of values. A transition that edited in place
    would leave the earlier states unrecoverable for evidence."""
    run = started()
    run.advance().advance()

    assert run.step_index == 0
    assert run.state is RunState.RUNNING
