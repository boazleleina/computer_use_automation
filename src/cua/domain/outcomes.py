"""What just happened, and how bad is it?

Five classes, and the distinctions between them are the ones that decide whether
a run retries, escalates, or answers. Getting the taxonomy wrong is worse than
getting a selector wrong: a wrong selector fails loudly, a wrong outcome class
reports "system error" for a member who simply does not exist.

classify() is pure. It takes what is on screen and the recognizers a capability
declared, and returns one classification. It reads no config, consults no model,
and has no opinion of its own: everything it knows came from the artifact. That
is what makes the artifact a complete account of how a run interprets the
application, rather than half the story with the rest buried in the engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only imports. conditions.py imports Outcome from here at runtime and
    # capability.py imports conditions, so pulling either back in at runtime
    # would close a cycle.
    from cua.domain.capability import SignalKind
    from cua.domain.conditions import Condition
    from cua.domain.observation import Observation


class Outcome(StrEnum):
    """The five classes, ordered below by severity rather than by name.

    SUCCESS               the capability did what it was asked
    BUSINESS_OUTCOME      the application answered, and the answer was no.
                          A member that does not exist, a rejected value. The
                          run worked; this is the result, not a failure.
    RECOVERABLE           nothing is wrong, something is in the way. Waiting or
                          dismissing it is expected to clear it.
    HARD_FAILURE          the system refused or broke in a way this run cannot
                          resolve. Debuggable, reportable, not retryable.
    INTERVENTION_REQUIRED a person is needed. Session expiry, an ambiguous
                          control, a screen nobody declared.
    """

    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"
    INTERVENTION_REQUIRED = "intervention_required"


# Ascending severity. When more than one condition matches, the worst wins.
SEVERITY: dict[Outcome, int] = {
    Outcome.SUCCESS: 0,
    Outcome.BUSINESS_OUTCOME: 1,
    Outcome.RECOVERABLE: 2,
    Outcome.HARD_FAILURE: 3,
    Outcome.INTERVENTION_REQUIRED: 4,
}


@dataclass(frozen=True)
class Classification:
    """One answer about one screen.

    `condition_name` is None when nothing matched. `matched` lists every
    condition that fired, not only the winner, so a success condition too weak
    to distinguish the screen it claimed shows up in the run record instead of
    passing unnoticed.
    """

    outcome: Outcome
    condition_name: str | None
    detail: str
    matched: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    """What a run hands back to whoever asked for it.

    Distinct from Classification, which is about one screen. This is about the
    whole run: the outcome, whatever the capability declared it would return,
    and enough context to debug a run nobody watched.

    `expected` and `observed` are filled in when a checkpoint fails. Together
    with the step id they turn "the run failed" into "submit_lookup expected the
    member detail heading and found the sign on page", which is the difference
    between a report somebody can act on and one they cannot.

    `resolved_via_by_step` records which signal matched for each target, keyed
    by step. Per step rather than one value for the run, because drift is a
    property of a target: a capability whose third step has started resolving on
    a weaker signal than it was compiled with is about to break there, and a
    single value for the whole run cannot say where.

    The same information goes to evidence as it happens. This is the summary a
    caller gets without reading the stream.
    """

    run_id: str
    capability: str
    version: str
    outcome: Outcome
    detail: str
    condition_name: str | None = None
    outputs: Mapping[str, object] = field(default_factory=dict)
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    evidence_ref: str | None = None
    resolved_via_by_step: Mapping[str, SignalKind] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether the capability did what it was asked.

        A business outcome is not ok and is not a failure either: the run
        worked and the answer was no. Callers that need that distinction read
        `outcome` rather than this.
        """
        return self.outcome is Outcome.SUCCESS


def classify(observation: Observation, conditions: tuple[Condition, ...]) -> Classification:
    """Decide what a screen means, using only what the capability declared.

    Precedence is by severity, never by declaration order. An expired session
    renders the login page at the route of the page it replaced, with HTTP 200,
    so a success condition keyed on the route matches at the same time as the
    session condition. Believing the success one is how an automation reports a
    balance it never read — and ordering conditions by hand is too easy to get
    wrong to be the thing standing between a member and that.

    An unmatched screen is INTERVENTION_REQUIRED, not a failure and certainly
    not a success. The domain does not know what happened, and the only honest
    thing to do with a screen nobody described is show it to a person.
    """
    matched = tuple(c for c in conditions if c.holds(observation))

    if not matched:
        return Classification(
            outcome=Outcome.INTERVENTION_REQUIRED,
            condition_name=None,
            detail="no declared condition describes this screen",
            matched=(),
        )

    winner = max(matched, key=lambda c: SEVERITY[c.outcome])
    return Classification(
        outcome=winner.outcome,
        condition_name=winner.name,
        detail=winner.detail,
        matched=tuple(c.name for c in matched),
    )

