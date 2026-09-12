"""A model that has already made up its mind.

Reads the `proposed` records out of a discovery transcript and hands them back
in order. Satisfies the Model port, so the discovery loop cannot tell it from
the real thing, and the loop, the policy checks and the compiler can all be
exercised without a network or a billing account.

This is not a model and must not be mistaken for one. It makes no decisions; it
repeats decisions a model already made against a real application. A green
suite here says the machinery around the model is correct, and says nothing at
all about whether the model can find its way through a screen it has not seen.
That question only a live run answers.

Targets are rebound rather than replayed. A recorded NodeRef names a node in an
observation that no longer exists, so matching by ref would fail on the first
step of any fresh session. Role and accessible name are matched against the
current screen instead — the same thing the ROLE_NAME signal does in an
artifact, and it fails loudly when the application has moved on.
"""

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cua.adapters.errors import ConfigurationError
from cua.domain.actions import ActionType, ProposalKind, ProposedAction
from cua.domain.errors import ModelError
from cua.domain.observation import Observation

PROPOSED = "proposed"


@dataclass
class RecordedModel:
    """Replays one transcript, one proposal per call."""

    proposals: Sequence["RecordedProposal"]
    _next: int = field(default=0, init=False)

    @classmethod
    def from_transcript(cls, path: Path) -> "RecordedModel":
        """Load the proposals out of a JSON Lines evidence stream.

        Everything that is not a `proposed` record is ignored rather than
        rejected. The stream carries observations, policy checks and actions
        too, and a loader that insisted on knowing every event type would break
        the next time the loop learns to record something new.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigurationError(f"could not read transcript {path}: {error}") from error

        proposals = [RecordedProposal.from_record(record) for record in _records(text, path)]
        if not proposals:
            raise ConfigurationError(f"{path} holds no {PROPOSED!r} records")
        return cls(proposals=proposals)

    def propose(
        self,
        goal: str,
        observation: Observation,
        history: Sequence[ProposedAction],
    ) -> ProposedAction:
        """The next recorded proposal, bound to the screen in front of it.

        `goal` and `history` are ignored on purpose. The decisions were made
        once, against a real application, by a real model; pretending to
        reconsider them here would make this look like a model and behave like
        a script, which is the confusing half of both.
        """
        if self._next >= len(self.proposals):
            raise ModelError(
                f"the transcript holds {len(self.proposals)} proposals and the run "
                "wants another; it did not end where the recorded one did"
            )

        recorded = self.proposals[self._next]
        self._next += 1
        return recorded.bind(observation)


@dataclass(frozen=True)
class RecordedProposal:
    """One `proposed` record, before it is tied to a screen."""

    kind: ProposalKind
    rationale: str
    action_type: ActionType | None
    value: str | None
    target_role: str | None
    target_name: str | None

    @classmethod
    def from_record(cls, record: dict[str, object]) -> "RecordedProposal":
        try:
            kind = ProposalKind(str(record["kind"]))
        except (KeyError, ValueError) as error:
            raise ConfigurationError(f"not a usable proposal record: {record}") from error

        action = record.get("action")
        return cls(
            kind=kind,
            rationale=str(record.get("rationale") or ""),
            action_type=ActionType(str(action)) if action else None,
            value=_optional(record.get("value")),
            target_role=_optional(record.get("target_role")),
            target_name=_optional(record.get("target_name")),
        )

    def bind(self, observation: Observation) -> ProposedAction:
        """Find the control this proposal named, on the screen as it is now.

        A recorded run that cannot be rebound is worth saying so about: it
        means the application changed under the transcript, and continuing
        would replay decisions made about a screen that is gone.
        """
        if self.target_role is None and self.target_name is None:
            return ProposedAction(
                kind=self.kind,
                rationale=self.rationale,
                action_type=self.action_type,
                value=self.value,
            )

        matches = [
            node
            for node in observation.nodes
            if node.role == self.target_role and node.name == self.target_name
        ]
        if len(matches) != 1:
            raise ModelError(
                f"the transcript names {self.target_role} {self.target_name!r}, "
                f"which matches {len(matches)} controls on {observation.url_pattern}"
            )

        return ProposedAction(
            kind=self.kind,
            rationale=self.rationale,
            action_type=self.action_type,
            node_ref=matches[0].ref,
            value=self.value,
        )


def _records(text: str, path: Path) -> Iterator[dict[str, object]]:
    """The `proposed` records, in the order they were written.

    A malformed line is fatal rather than skipped. A transcript with a hole in
    it replays a different run from the one it recorded, and silently doing
    that is worse than refusing.
    """
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ConfigurationError(f"{path} line {number} is not JSON: {error}") from error
        if isinstance(record, dict) and record.get("event") == PROPOSED:
            yield record


def _optional(value: object) -> str | None:
    return None if value is None else str(value)
