"""How a capability describes the control it means to act on.

A TargetSpec is a ranked bundle of ways to describe one control, most portable
first. It exists because no single description survives a legacy application:
an accessible name is stable until someone rewords a label, geometry is stable
until the layout moves, and a CSS selector is not a description of the control
at all, only of the markup that happens to render it.

Ranking them and recording which one actually fired turns "this capability
broke" into "this capability has been resolving on its third choice for a
week", which is a thing you can act on before it breaks.

A Capability is the written down procedure: a contract saying what it takes and
returns, steps saying how, and conditions saying what the screens mean. It is
the unit that gets approved, versioned and replayed, and it is the reason the
model only runs once.
"""

from dataclasses import dataclass
from enum import StrEnum

from cua.domain.actions import ActionType, Effect
from cua.domain.conditions import Condition, Detector
from cua.domain.errors import MalformedArtifact, UnsafeCapability
from cua.domain.observation import Rect
from cua.domain.policy import Sensitivity


class SignalKind(StrEnum):
    """Ways of describing a control, in the order they should be preferred.

    ROLE_NAME   role plus accessible name. Portable across web and desktop.
    LABEL       accessible name alone, role ignored. Survives a control being
                re-rendered as a different widget.
    ANCHOR      a nearby node plus a relation. The only way to reach a value
                whose own name is the value.
    GEOMETRY    position on screen. Portable in principle, brittle in practice;
                carry it only with brittle metadata set.
    WEB_CSS     namespaced escape hatch. Deliberately not called "css": it has
                no meaning on a desktop surface, and naming it honestly is what
                keeps the portability claim true.
    """

    ROLE_NAME = "role_name"
    LABEL = "label"
    ANCHOR = "anchor"
    GEOMETRY = "geometry"
    WEB_CSS = "web.css"


class Confidence(StrEnum):
    """How much the compiler trusts a signal.

    Not consulted by resolve(): resolution takes signals in the order the spec
    lists them. This is for the human reviewing an artifact and for the
    compiler recording why it ranked one description above another.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Relation(StrEnum):
    """How to get from an anchor node to the node actually wanted."""

    NEXT_SIBLING = "next_sibling"


@dataclass(frozen=True)
class Signal:
    """One way of describing a control.

    `brittle` is metadata, not an input to resolution. A signal marked brittle
    resolved correctly today and is expected to stop doing so.
    """

    kind: SignalKind
    confidence: Confidence
    role: str | None = None
    name: str | None = None
    relation: Relation | None = None
    bounds: Rect | None = None
    brittle: bool = False


@dataclass(frozen=True)
class TargetSpec:
    """A ranked bundle of signals describing one control.

    `intent` says what the control is for in human terms, and `rationale` says
    why the compiler chose these signals. Both exist so an artifact reads as a
    procedure a person approved rather than a machine's scratch notes.
    """

    intent: str
    rationale: str
    signals: tuple[Signal, ...]


class Approval(StrEnum):
    """Whether this version has been signed off.

    A hand written or freshly discovered artifact is a draft. Approval is what
    a person adds after reading it, and it is per version: editing a step and
    keeping the approval would defeat the point of recording one.
    """

    DRAFT = "draft"
    APPROVED = "approved"


@dataclass(frozen=True)
class Provenance:
    """Where this artifact came from, and what it was written against.

    A capability that resolved cleanly against release 4.2.11 says nothing about
    4.3, and the first sign of that is usually resolution falling to a weaker
    signal. Recording the release makes that comparison possible instead of
    guesswork.
    """

    source: str
    author: str = ""
    app: str = ""
    release: str = ""
    variant: str = ""
    note: str = ""


@dataclass(frozen=True)
class InputSpec:
    """One value the capability takes.

    `sensitivity` is declared here, once, and everything downstream reads it
    from this one place. That is what makes redaction a property of the
    contract instead of a habit at each call site.
    """

    name: str
    type: str
    sensitivity: Sensitivity
    required: bool = True
    pattern: str | None = None
    description: str = ""


@dataclass(frozen=True)
class OutputSpec:
    """One value the capability returns."""

    name: str
    type: str
    sensitivity: Sensitivity
    transform: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Contract:
    """What the capability takes, returns, and does to the data behind it.

    `effect` is the risk class. It governs whether the capability may run
    unattended, which is a separate question from whether any individual action
    is allowed.
    """

    name: str
    version: str
    goal: str
    effect: Effect
    approval: Approval = Approval.DRAFT
    preconditions: tuple[str, ...] = ()
    inputs: tuple[InputSpec, ...] = ()
    outputs: tuple[OutputSpec, ...] = ()
    provenance: Provenance | None = None

    @property
    def approved(self) -> bool:
        return self.approval is Approval.APPROVED

    def __post_init__(self) -> None:
        names = [output.name for output in self.outputs]
        duplicated = sorted({name for name in names if names.count(name) > 1})
        if duplicated:
            raise MalformedArtifact(
                f"output(s) {duplicated} are declared more than once; a caller "
                "handed two values under one name cannot tell which it got"
            )

        """A secret must not be an interface value of a capability.

        Checked here rather than at load time because this is not an authoring
        mistake that shows up on the first run. A contract declaring a secret
        input parses, validates, and works: the compiler would parameterise it,
        replay would bind it, and a step would type it. That is replay holding a
        credential, which is the thing the handover design exists to prevent.

        The same rule from the other side covers outputs. A secret returned to a
        caller has crossed the boundary already, and redaction would drop it on
        the way to evidence, so the capability would be declaring something it
        can never hand back.

        Credentials reach the application through a person, at a keyboard, in a
        browser the run is already driving. They are never an argument.
        """
        offenders = [spec.name for spec in self.inputs if spec.sensitivity is Sensitivity.SECRET]
        offenders += [spec.name for spec in self.outputs if spec.sensitivity is Sensitivity.SECRET]
        if offenders:
            raise UnsafeCapability(
                f"capability {self.name!r} declares secret field(s) {offenders}; "
                "a credential is entered by a person during handover, never passed to a run"
            )


@dataclass(frozen=True)
class Step:
    """One instruction in the procedure.

    `target` is None for NAVIGATE, which addresses a route rather than a
    control. `value` may carry a placeholder such as {{ inputs.member_id }},
    bound at replay time so the artifact holds no member's data.

    `checkpoint` is the condition that has to hold once the step has run.
    Without it a step that silently did nothing looks the same as one that
    worked, which is how a run reports a balance it never read.
    """

    id: str
    action_type: ActionType
    target: TargetSpec | None = None
    value: str | None = None
    checkpoint: tuple[Detector, ...] = ()
    reads_into: str | None = None


@dataclass(frozen=True)
class Capability:
    """A procedure that has been written down, and can therefore be approved.

    Three layers, kept separate because they answer different questions and are
    reviewed by different people: the contract says what this is for, the steps
    say how it is done, and the conditions say what the application's replies
    mean.
    """

    contract: Contract
    steps: tuple[Step, ...] = ()
    conditions: tuple[Condition, ...] = ()
    success: tuple[Detector, ...] = ()

    def declared_sensitivity(self) -> dict[str, Sensitivity]:
        """Field name to sensitivity, for every input and output.

        This is what redaction is driven from. Anything not named here is
        undeclared and gets dropped from evidence rather than published.
        """
        declared = {spec.name: spec.sensitivity for spec in self.contract.inputs}
        declared.update({spec.name: spec.sensitivity for spec in self.contract.outputs})
        return declared

    def step(self, step_id: str) -> Step:
        """One step by its declared id.

        Steps are addressed by name rather than position because a run record
        saying "submit_lookup failed" is something a person can act on, and
        "step 1 failed" is not. Order still comes from the list.
        """
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(f"{self.contract.name} has no step {step_id!r}")
