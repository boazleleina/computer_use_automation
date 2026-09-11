"""The only port that reasons. Used by discovery, and by nothing else.

ReplayCapability never receives an implementation of this protocol. Replay
cannot consult a model because nothing in its dependency graph can reach one,
which makes that a property of the wiring rather than a rule someone has to
remember.

The port returns a validated ProposedAction, never text. Prose is not a control
instruction, and parsing it as one would move the decision about what the system
does out of code that can be reviewed.
"""

from collections.abc import Sequence
from typing import Protocol

from cua.domain.actions import ProposedAction
from cua.domain.observation import Observation


class Model(Protocol):
    """Propose the next step toward a goal, given what is on screen."""

    def propose(
        self,
        goal: str,
        observation: Observation,
        history: Sequence[ProposedAction],
    ) -> ProposedAction:
        """Propose one step.

        `history` is passed explicitly rather than held inside the
        implementation, so the same call with the same inputs is reproducible
        and a recorded transcript can be replayed against it.

        Implementations own schema validation and malformed output retry. A
        caller receives a well formed proposal or a ModelError, never a partial
        one it has to sanity check itself.
        """
        ...
