"""Asking a person to take over, and recording what they did.

Two types, and the split between them is the phase in miniature: one describes
a run that has stopped and what would have to be true for it to continue, and
the other describes what happened while somebody else held it.

Neither of them can reach a browser, a console or a queue. An intervention
request is a statement of fact that some implementation delivers somehow — a
terminal prompt here, a ticket or a websocket later — and keeping it a plain
value is what stops the delivery mechanism deciding what a request contains.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from cua.domain.observation import Observation
from cua.domain.policy import REDACTED
from cua.domain.run import EscalationReason

__all__ = ["REDACTED", "Handover", "HumanAction", "HumanEvent", "InterventionRequest"]


class HumanAction(StrEnum):
    """What a person did while they held the session.

    Coarse on purpose. The point of recording this is to answer "what happened
    to the application while automation was not looking", and that question is
    answered by which controls were touched and where the session went. A
    keystroke-level record would be a surveillance log of an employee doing
    their job, which is a different thing and not one this system should hold.
    """

    CLICK = "click"
    NAVIGATE = "navigate"
    INPUT = "input"
    SENSITIVE_INPUT = "sensitive_input"


@dataclass(frozen=True)
class HumanEvent:
    """One thing a person did, already sanitised.

    Sanitised at capture rather than on the way to evidence. A credential that
    reaches a domain object has already been in memory somewhere it can be
    logged by accident, and the whole point of the handover is that the
    automation never holds one. By the time a HumanEvent exists, the value of a
    sensitive input is the string REDACTED and the original is gone.

    `target` is what the control is called, never a selector: the same
    vocabulary the rest of the system reasons in, so a handover record can be
    read beside a run record without translation.
    """

    action: HumanAction
    target: str | None = None
    value: str | None = None
    route: str | None = None

    @property
    def sensitive(self) -> bool:
        return self.action is HumanAction.SENSITIVE_INPUT


@dataclass(frozen=True)
class InterventionRequest:
    """Everything a person needs to pick up a stopped run.

    Assembled by the engine at the moment it stops, because that is the only
    moment the answer is knowable: the screen that caused the decision, the
    step that was being attempted, the rule or condition that fired. Rebuilding
    it later from a log would be reconstruction, and the difference shows up
    exactly when it matters, which is when something went wrong.

    `resume_checkpoint` is the contract of the handover. It names what has to
    be true before the automation may act again — an authenticated session, a
    particular screen — and it is checked on return rather than assumed. A
    person is free to wander: they might sign on and go and look at something
    else, and the run must not continue on the strength of them having said
    they were finished.
    """

    run_id: str
    capability: str
    version: str
    goal: str
    reason: EscalationReason
    detail: str
    resume_checkpoint: str
    step_id: str | None = None
    observation: Observation | None = None
    screenshot_ref: str | None = None

    def summary(self) -> dict[str, object]:
        """The request as a flat record, for evidence and for display.

        The Observation is reduced to what a person needs to recognise the
        screen — where they are and what it is called. The whole node list is
        the engine's business and would bury the four fields that matter.
        """
        return {
            "run_id": self.run_id,
            "capability": f"{self.capability} v{self.version}",
            "goal": self.goal,
            "reason": self.reason.value,
            "detail": self.detail,
            "step": self.step_id,
            "resume_checkpoint": self.resume_checkpoint,
            "url_pattern": self.observation.url_pattern if self.observation else None,
            "page_title": self.observation.page_title if self.observation else None,
            "screenshot": self.screenshot_ref,
        }


@dataclass(frozen=True)
class Handover:
    """What came back from the person who held the run.

    A list rather than a bare signal, because "the human says they are done" is
    not the same as "the run may continue" and the engine has to be able to
    tell the difference. What they did is recorded whether or not the run goes
    on to succeed.
    """

    events: tuple[HumanEvent, ...] = field(default_factory=tuple)

    def records(self) -> list[dict[str, object]]:
        """The events as evidence records, in order."""
        return [
            {
                "action": event.action.value,
                "target": event.target,
                "value": event.value,
                "route": event.route,
            }
            for event in self.events
        ]
