"""The closed vocabulary of things the system can do or propose.

The enums, plus the one structure the Model port needs in its signature.
Action and ActResult land with the replay engine.

These are the enums rather than free strings because both ends depend on the set
being closed: Policy's allowlist is expressed over ActionType, and the model's
tool schema is generated from it, so an out-of-vocabulary action cannot be
proposed in the first place and is refused again before it reaches the surface.
"""

from dataclasses import dataclass
from enum import StrEnum

from cua.domain.observation import NodeRef


class ActionType(StrEnum):
    """What can be done to a control.

    Values are lowercase so they read the same in config.yaml, in a capability
    artifact, and in the model's tool schema.
    """

    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    NAVIGATE = "navigate"
    READ = "read"


class Effect(StrEnum):
    """What a capability does to the data behind the screen.

    Three members, and deliberately not four. Signing off destroys a run's
    ability to continue — replay cannot recover, because recovery means typing a
    password and replay is forbidden from touching credentials — but it changes
    no member record, so it is READ_ONLY by data effect and always will be.

    Continuity risk is a different axis and is handled on that axis: policy
    refuses named controls outright, whatever their effect class. Folding the
    two into one enum would force a lie about any control that is both, and
    would make "is this capability safe to run unattended" and "does this
    capability change anything" the same question. They are not.
    """

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    IRREVERSIBLE = "irreversible"


class ProposalKind(StrEnum):
    """What the model may say next.

    Three, and only three. A proposal is structured output validated against
    this set, never prose interpreted as an instruction.
    """

    ACT = "act"
    COMPLETE = "complete"
    STUCK = "stuck"


@dataclass(frozen=True)
class ProposedAction:
    """One validated proposal from the model. Not yet permitted, not yet done.

    Named for what it is: the model proposes, Policy disposes, and only then
    does the surface act. Keeping the proposal a distinct type from the action
    that executes is what makes "every proposal was checked" auditable.

    `rationale` is required. A one-sentence reason per step is what makes a
    discovery log readable months later, and it costs one line of model output.
    """

    kind: ProposalKind
    rationale: str
    action_type: ActionType | None = None
    node_ref: NodeRef | None = None
    value: str | None = None
