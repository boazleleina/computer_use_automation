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

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from cua.domain.observation import Observation
from cua.domain.outcomes import Outcome


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
    URL_PATTERN = "url_pattern"
    TITLE_IS = "title_is"


@dataclass(frozen=True)
class Detector:
    """One observable fact about a screen."""

    kind: DetectorKind
    text: str | None = None
    role: str | None = None
    name: str | None = None
    url_pattern: str | None = None
    title: str | None = None

    def holds(self, observation: Observation) -> bool:
        """Whether this fact is true of the screen.

        Text comparison is case insensitive because Observation.all_text()
        lowercases, and a condition author should not have to reproduce the
        application's capitalisation to write a working recognizer.
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
