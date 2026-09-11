"""Handing control to a human, and taking it back.

A port because escalation is an external side effect with more than one
plausible implementation — a local prompt now, an HTTP callback later — and
because a fake operator is what lets the handoff path be tested without a human.

Present in both discovery and replay. Escalation is not a discovery-only
concern: a replay that meets an expired session needs a person exactly as much
as a discovery run that meets an ambiguous control.

While the human owns the run, automation issues no actions. That rule is
enforced by the run state model, not by this port; the port only reports the
transfer.
"""

from typing import Protocol

from cua.domain.observation import Observation


class OperatorChannel(Protocol):
    """Request human intervention and wait for control to return."""

    def request_intervention(
        self,
        run_id: str,
        reason: str,
        observation: Observation,
        screenshot_ref: str | None = None,
    ) -> None:
        """Publish a request for a human to take over.

        Takes an Observation rather than a rendered message so the
        implementation decides how to present it. `screenshot_ref` points at
        evidence already stored, so an image is not carried around in memory.

        These parameters collapse into an InterventionRequest once the run
        state model exists, carrying the capability id, the current step, and
        the condition that has to hold before the run may continue.
        """
        ...

    def await_release(self, run_id: str) -> None:
        """Block until the human returns control.

        Returning does not mean the run may continue. The caller must
        re-observe and verify its checkpoint first, because the human may have
        left the application anywhere.
        """
        ...
