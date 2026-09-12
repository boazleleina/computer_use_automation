"""Who owns the run, and how control moves between them.

A Run is a value. Every transition returns a new one, so the history of a run is
a sequence that can be written to evidence rather than a mutable object whose
earlier states are gone by the time anyone looks.

The path through an escalation:

    RUNNING -> PAUSED -> HUMAN_CONTROL -> RESUMING -> RUNNING

and only RUNNING permits the automation to act. That is the point of having the
states at all: while a person holds the session, `may_act` is False, so an
engine that forgets to check gets no action rather than a race with the human.

Nothing here re-authenticates. Replay never types a credential, so an expired
session is a request for a person, not something to retry.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from cua.domain.errors import InvalidTransition

# How many times one problem may pull an operator back into the same run.
# Once is a session that aged out. Twice means the session is shorter than the
# flow takes, or the application is invalidating it on every step, and asking
# the operator again wastes their time without fixing anything.
ESCALATIONS_PER_REASON = 1

# How many times one step may be retried after a recoverable condition. Counted
# per step, so a slow page early in a flow does not spend the budget for an
# interstitial later. Exhausting it escalates rather than fails: waiting has
# stopped working, but a person may still be able to clear whatever is stuck.
RECOVERIES_PER_STEP = 2


class RunState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    HUMAN_CONTROL = "human_control"
    RESUMING = "resuming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_STATES = frozenset({RunState.SUCCEEDED, RunState.FAILED})


class Owner(StrEnum):
    """Who may issue actions. Never both."""

    AUTOMATION = "automation"
    HUMAN = "human"


class EscalationReason(StrEnum):
    """Why a person was asked for.

    Counted separately so that spending the budget on one problem does not
    silence a different one later in the same run.
    """

    AUTHENTICATION_REQUIRED = "authentication_required"
    AMBIGUOUS_CONTROL = "ambiguous_control"
    TARGET_NOT_FOUND = "target_not_found"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    UNKNOWN_STATE = "unknown_state"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True)
class Run:
    """One execution of one capability.

    `resume_checkpoint` names the condition that has to hold before the
    automation may take the session back. It is a precondition, not a receipt:
    after a handover the screen is whatever the operator left behind, and the
    step is retried from the top rather than continued from the middle.
    """

    run_id: str
    capability: str
    version: str
    state: RunState = RunState.RUNNING
    owner: Owner = Owner.AUTOMATION
    step_index: int = 0
    escalations: Mapping[EscalationReason, int] = field(default_factory=dict)
    recoveries: Mapping[int, int] = field(default_factory=dict)
    resume_checkpoint: str | None = None
    failure_reason: str = ""

    @classmethod
    def start(cls, run_id: str, capability: str, version: str) -> "Run":
        return cls(run_id=run_id, capability=capability, version=version)

    @property
    def may_act(self) -> bool:
        """Whether the automation may issue an action right now.

        RUNNING and owned by the automation. Paused, held by a person, or part
        way back from a handover all answer no.
        """
        return self.state is RunState.RUNNING and self.owner is Owner.AUTOMATION

    @property
    def awaiting_operator(self) -> bool:
        """Whether a request for a person has been raised and not yet picked up.

        Paused is the only state where that is true: before it nobody has been
        asked, and after the handover somebody already has it.
        """
        return self.state is RunState.PAUSED

    def escalation_count(self, reason: EscalationReason) -> int:
        return self.escalations.get(reason, 0)

    def recovery_count(self, step_index: int | None = None) -> int:
        """Retries already spent on a step. Defaults to the current one."""
        return self.recoveries.get(self.step_index if step_index is None else step_index, 0)

    def recover(self) -> "Run":
        """Retry the current step after a recoverable condition.

        Bounded per step. When the budget is gone, waiting has demonstrably
        stopped working, so the run asks for a person rather than looping: an
        interstitial that will not clear and a page that never finishes loading
        both look like this, and both need someone to look.
        """
        self._require(RunState.RUNNING, "recover")

        if self.recovery_count() >= RECOVERIES_PER_STEP:
            return self.escalate(
                EscalationReason.RECOVERY_EXHAUSTED,
                resume_checkpoint="step_precondition",
            )

        counts = dict(self.recoveries)
        counts[self.step_index] = self.recovery_count() + 1
        return replace(self, recoveries=MappingProxyType(counts))

    def advance(self) -> "Run":
        """Move to the next step of the capability."""
        self._require(RunState.RUNNING, "advance")
        return replace(self, step_index=self.step_index + 1)

    def start_over(self) -> "Run":
        """Go back to the first step, keeping what the run has spent.

        For after a handover, where the reason the run stopped is also a reason
        its earlier steps no longer hold. Signing back on returns an empty
        search page: the member number a previous step typed is gone, so
        resuming at the step that was interrupted would submit an empty search
        and report the application broken.

        The budgets do not reset. Escalations and recoveries already spent stay
        spent, which is what stops a run that can be rescued once from being
        rescued indefinitely — the second escalation for the same reason fails
        the run rather than asking again.

        Whether starting over is safe at all is not this object's judgement. A
        capability that has already submitted something must not silently do it
        twice, and the caller decides that from the capability's effect.
        """
        self._require(RunState.RUNNING, "start_over")
        return replace(self, step_index=0)

    def escalate(self, reason: EscalationReason, resume_checkpoint: str) -> "Run":
        """Ask for a person, or give up if this problem has already had its turn."""
        self._reject_if_terminal("escalate")
        self._require(RunState.RUNNING, "escalate")

        already = self.escalation_count(reason)
        if already >= ESCALATIONS_PER_REASON:
            return self.fail(
                f"{reason.value} escalated more than {ESCALATIONS_PER_REASON} time(s); "
                "a second request for the same problem would not fix it"
            )

        counts = dict(self.escalations)
        counts[reason] = already + 1
        return replace(
            self,
            state=RunState.PAUSED,
            escalations=MappingProxyType(counts),
            resume_checkpoint=resume_checkpoint,
        )

    def hand_over(self) -> "Run":
        """Give the session to the operator.

        Only reachable from PAUSED, so there is always a recorded reason behind
        a person holding the session.
        """
        self._require(RunState.PAUSED, "hand_over")
        return replace(self, state=RunState.HUMAN_CONTROL, owner=Owner.HUMAN)

    def return_control(self) -> "Run":
        """The operator has finished. The run still may not act.

        What is on screen now is unknown: the operator may have signed in and
        gone somewhere else entirely. Verifying that is a separate step.
        """
        self._require(RunState.HUMAN_CONTROL, "return_control")
        return replace(self, state=RunState.RESUMING, owner=Owner.AUTOMATION)

    def resume(self, checkpoint_holds: bool) -> "Run":
        """Take the session back, if the checkpoint holds.

        `step_index` is unchanged. Everything resolved before pausing belongs to
        an observation that no longer describes the screen, so the step runs
        again from observation rather than picking up where it stopped.
        """
        self._require(RunState.RESUMING, "resume")
        if not checkpoint_holds:
            return self.fail(
                f"resume checkpoint {self.resume_checkpoint!r} did not hold after handover"
            )
        return replace(self, state=RunState.RUNNING, resume_checkpoint=None)

    def succeed(self) -> "Run":
        self._reject_if_terminal("succeed")
        return replace(self, state=RunState.SUCCEEDED)

    def fail(self, reason: str) -> "Run":
        self._reject_if_terminal("fail")
        return replace(self, state=RunState.FAILED, failure_reason=reason)

    def _require(self, expected: RunState, action: str) -> None:
        self._reject_if_terminal(action)
        if self.state is not expected:
            raise InvalidTransition(
                f"cannot {action} from {self.state.value}; expected {expected.value}"
            )

    def _reject_if_terminal(self, action: str) -> None:
        if self.state in TERMINAL_STATES:
            raise InvalidTransition(f"cannot {action} a run that has already {self.state.value}")
