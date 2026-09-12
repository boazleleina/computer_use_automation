"""Handing control to a human, and taking it back.

A port because escalation is an external side effect with more than one
plausible implementation — a terminal prompt now, an HTTP callback later — and
because a fake operator is what lets the handoff path be tested without a
person sitting there.

Present in both discovery and replay. Escalation is not a discovery-only
concern: a replay that meets an expired session needs a person exactly as much
as a discovery run that meets an ambiguous control.

While the human owns the run, automation issues no actions. That rule is
enforced by the run state model, not by this port; the port only reports the
transfer. Run.may_act is false in HUMAN_CONTROL and false again in RESUMING,
so even an engine that forgot to wait could not act during a handover.

What an implementation must supply, and the reason each is not optional:

    the same session     A person asked to fix a run has to be looking at the
                         run's own browser. A fresh window is a different
                         session with a different cookie, and signing on there
                         leaves the automation exactly as locked out as before.

    what they did        Automation stops looking during a handover, so without
                         a record nobody can say what changed underneath it.
                         Sanitised at capture: a credential must not reach a
                         domain object at all, so the value of a sensitive
                         input is already REDACTED by the time it is returned.
"""

from collections.abc import Sequence
from typing import Protocol

from cua.domain.intervention import Handover, HumanEvent, InterventionRequest


class OperatorChannel(Protocol):
    """Request human intervention and wait for control to return."""

    def request_intervention(self, request: InterventionRequest) -> None:
        """Publish a request for a human to take over.

        One assembled value rather than a handful of parameters, so what a
        request contains is decided by the domain and not by whichever
        implementation happens to be delivering it. The request carries the
        screen that caused the decision rather than a fresh look at the world:
        on a live browser those differ, and showing an operator a page that has
        since settled would show them something the engine never saw.
        """
        ...

    def await_release(self, run_id: str) -> Handover:
        """Block until the human returns control, and report what they did.

        Returning does not mean the run may continue. The caller must
        re-observe and verify the resume checkpoint first, because the human
        may have signed on and then gone somewhere else entirely, and "I am
        finished" is not the same claim as "the application is where you left
        it".

        The Handover is returned whether or not the run goes on to succeed. A
        record of what a person did to a member's account is worth keeping
        particularly when the run failed afterwards.
        """
        ...


class HumanActivity(Protocol):
    """Watching a live session while somebody else is driving it.

    Separate from OperatorChannel because they are different jobs that happen
    to be needed together: one delivers a request and waits, the other observes
    a surface. A console operator watching a browser needs both, and a queue
    based operator watching the same browser would reuse this unchanged.
    """

    def start(self) -> None:
        """Begin recording. Called once control has been ceded, never before:
        actions the automation performed are already in the run record and
        would be counted twice."""
        ...

    def stop(self) -> Sequence[HumanEvent]:
        """Stop recording and hand back what was seen, sanitised."""
        ...
