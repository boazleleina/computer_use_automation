"""Declared recognizers: how a capability says what a screen means.

A Condition is data, not code. It lives in the artifact and states in as many
words that this capability treats "No member matches" as a legitimate business
outcome rather than a crash. Recognition is declared rather than inferred
because the artifact is approved before it runs unattended, and a decision the
model makes at runtime cannot be approved in advance.

Detectors are deliberately few. Every kind here can be evaluated against an
Observation alone — no DOM, no driver, no network — which is what keeps
classification pure and testable with no browser installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from cua.domain.observation import NodeRef, Observation
from cua.domain.outcomes import Outcome

if TYPE_CHECKING:
    # capability.py imports Condition at runtime for Step.checkpoint, so
    # importing TargetSpec back would close a cycle.
    from cua.domain.capability import TargetSpec


class DetectorKind(StrEnum):
    """Ways of recognising a screen.

    TEXT_PRESENT   the words appear somewhere on the page
    TEXT_ABSENT    they do not. Useful for "the error banner is gone"
    NODE_PRESENT   a control with this role and name exists
    URL_PATTERN    the route matches. Never sufficient alone: an expired
                   session serves the login page at the route it replaced
    TITLE_IS       the page title matches exactly
    """

    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    NODE_PRESENT = "node_present"
    URL_PATTERN = "url_pattern_is"
    TITLE_IS = "title_is"
    FIELD_VALUE_EQUALS = "field_value_equals"


@dataclass(frozen=True)
class Detector:
    """One observable fact about a screen."""

    kind: DetectorKind
    text: str | None = None
    role: str | None = None
    name: str | None = None
    url_pattern: str | None = None
    title: str | None = None
    value: str | None = None
    target_ref: str | None = None

    def holds(self, observation: Observation, subject: NodeRef | None = None) -> bool:
        """Whether this fact is true of the screen.

        Text comparison is case insensitive because Observation.all_text()
        lowercases, and a condition author should not have to reproduce the
        application's capitalisation to write a working recognizer.

        `subject` is the node the step just acted on. Only FIELD_VALUE_EQUALS
        uses it: a step that types into a field is checked by reading that same
        field back, and "that same field" is not something a page-wide detector
        can work out for itself. Page conditions pass nothing and ignore it.
        """
        if self.kind is DetectorKind.TEXT_PRESENT:
            return self.text is not None and self.text.lower() in observation.all_text()

        if self.kind is DetectorKind.TEXT_ABSENT:
            return self.text is not None and self.text.lower() not in observation.all_text()

        if self.kind is DetectorKind.NODE_PRESENT:
            return any(
                node.role == self.role and node.name == self.name for node in observation.nodes
            )

        if self.kind is DetectorKind.URL_PATTERN:
            return observation.url_pattern == self.url_pattern

        if self.kind is DetectorKind.TITLE_IS:
            return observation.page_title == self.title

        if self.kind is DetectorKind.FIELD_VALUE_EQUALS:
            if subject is None or self.value is None:
                return False
            node = next((n for n in observation.nodes if n.ref == subject), None)
            return node is not None and node.text == self.value

        # Exhaustive over DetectorKind. A new kind that nobody implemented is a
        # type error, not a detector that silently never holds — which would
        # make a condition quietly unmatchable and a screen quietly unknown.
        assert_never(self.kind)


@dataclass(frozen=True)
class Condition:
    """A named recognizer bound to an outcome class.

    `name` appears in evidence, so it reads as a thing that happened:
    MEMBER_NOT_FOUND, LOGIN_REQUIRED. `detail` is the capability's own sentence
    about it, carried through to the run record, so the record explains itself
    in the words of whoever wrote the capability rather than the engine's.
    """

    name: str
    outcome: Outcome
    detectors: tuple[Detector, ...]
    detail: str = ""
    code: str | None = None
    resume_checkpoint: str | None = None
    recovery: Recovery | None = None

    def holds(self, observation: Observation, subject: NodeRef | None = None) -> bool:
        """Every detector must hold. Detectors within a condition are ANDed.

        Deliberately not ORed: a condition describes one screen, and a
        description that accepts a screen matching only part of it is the weak
        success condition that fires on a sign-on page served at the route it
        replaced.
        """
        return all(detector.holds(observation, subject) for detector in self.detectors)


class RecoveryAction(StrEnum):
    """How to clear something that is merely in the way.

    Deliberately not ActionType. Dismissing an interstitial happens to be a
    click, and waiting is not an action on the application at all; describing
    both as surface actions would put the engine's strategy into a vocabulary
    meant for what a person does to a control.

    WAIT     let it settle and look again. For a page still loading.
    DISMISS  act on the control that clears it, then look again.
    """

    WAIT = "wait"
    DISMISS = "dismiss"


@dataclass(frozen=True)
class Recovery:
    """How a recoverable condition is cleared, and how many times to try.

    Carried by the condition that detects the problem rather than by the step,
    because the same interstitial can appear on any step and the way to dismiss
    it does not change.

    `max_attempts` is the artifact's own bound. Nothing stops a page reappearing
    forever, so the bound is what turns "wait and retry" into something that
    terminates.
    """

    action: RecoveryAction
    max_attempts: int = 2
    wait_ms: int = 1000
    target: TargetSpec | None = None
