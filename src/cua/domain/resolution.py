"""Which control is this?

A pure function over an Observation and a TargetSpec. No I/O, no driver, no
model. It answers with one of three outcomes, and two of them are refusals.

The refusals are the point. In a banking back office, resolving to the wrong
control means typing an account number into another member's form, so a
resolution that is not certain must decline rather than pick.

DOCUMENT ORDER IS A CONTRACT. Relation.NEXT_SIBLING is implemented as index + 1
into Observation.nodes. That is correct only while every SurfacePort
implementation yields nodes in document order, and nothing in the type system
enforces it. tests/unit/test_resolution.py pins the assumption against the
captured fixtures so that a surface which reorders nodes fails a test rather
than silently reading the cell next door.
"""

from dataclasses import dataclass
from typing import TypeAlias, assert_never

from cua.domain.capability import Relation, Signal, SignalKind, TargetSpec
from cua.domain.observation import Node, NodeRef, Observation, Rect

# How far apart two boxes may sit and still be considered the same control.
GEOMETRY_TOLERANCE_PX = 4.0


@dataclass(frozen=True)
class Resolved:
    """Exactly one control matched.

    `signal_index` alongside `via_signal` because a spec may list two signals of
    the same kind. Knowing resolution has started falling to the second one is
    the warning that arrives before a capability breaks.
    """

    ref: NodeRef
    via_signal: SignalKind
    signal_index: int
    skipped: tuple[SignalKind, ...] = ()


@dataclass(frozen=True)
class Ambiguous:
    """More than one control matched.

    Carries no ref, deliberately. There is nothing here for a caller to reach
    for, so "it picked one anyway" is not a bug that can be written.
    """

    count: int
    at_signal: SignalKind
    signal_index: int
    skipped: tuple[SignalKind, ...] = ()


@dataclass(frozen=True)
class NotFound:
    """No signal in the spec matched anything."""

    skipped: tuple[SignalKind, ...] = ()


Resolution: TypeAlias = Resolved | Ambiguous | NotFound


def resolve(
    spec: TargetSpec,
    observation: Observation,
    supported_kinds: frozenset[SignalKind],
) -> Resolution:
    """Find the one control `spec` describes, or refuse.

    Signals are tried in the order the spec ranks them. An ambiguity stops the
    search rather than falling through to a weaker signal: a lower ranked
    description that happens to match one node would resolve confidently to a
    control the better description could not distinguish, which is how you act
    on the wrong account and record high confidence while doing it.
    """
    skipped: list[SignalKind] = []

    for index, signal in enumerate(spec.signals):
        if not _evaluable(signal.kind, supported_kinds):
            skipped.append(signal.kind)
            continue

        matches = _matches(signal, observation)

        if len(matches) > 1:
            return Ambiguous(
                count=len(matches),
                at_signal=signal.kind,
                signal_index=index,
                skipped=tuple(skipped),
            )

        if len(matches) == 1:
            target = _follow(signal.relation, observation, matches[0][0])
            if target is None:
                # The anchor matched but there is nothing after it. Treat it as
                # this signal failing, not as the whole spec failing, so a
                # lower ranked signal still gets its turn.
                continue
            return Resolved(
                ref=target.ref,
                via_signal=signal.kind,
                signal_index=index,
                skipped=tuple(skipped),
            )

    return NotFound(skipped=tuple(skipped))


def _evaluable(kind: SignalKind, supported_kinds: frozenset[SignalKind]) -> bool:
    """Whether this domain can decide the signal at all.

    Two separate reasons to skip. The surface may not support the kind, and
    web.css is never decidable here whatever the surface says: evaluating it
    needs a selector engine, and the domain holds no selectors by design.
    """
    if kind is SignalKind.WEB_CSS:
        return False
    return kind in supported_kinds


def _matches(signal: Signal, observation: Observation) -> list[tuple[int, Node]]:
    """Candidate nodes, with their index, so a relation can step from one.

    Visibility and enabled state are not filtered here. A hidden control that
    matches is a genuine ambiguity in the page, and hiding it from resolution
    would turn a refusal into a confident wrong answer.
    """
    candidates = list(enumerate(observation.nodes))

    if signal.kind is SignalKind.ROLE_NAME:
        return [(i, n) for i, n in candidates if n.role == signal.role and n.name == signal.name]

    if signal.kind is SignalKind.LABEL:
        # Role deliberately ignored: a control re-rendered as a different widget
        # keeps its label, and that is the whole reason this kind exists.
        return [(i, n) for i, n in candidates if n.name == signal.name]

    if signal.kind is SignalKind.ANCHOR:
        return [(i, n) for i, n in candidates if n.role == signal.role and n.name == signal.name]

    if signal.kind is SignalKind.GEOMETRY:
        if signal.bounds is None:
            return []
        return [(i, n) for i, n in candidates if _near(n.bounds, signal.bounds)]

    return []


def _follow(relation: Relation | None, observation: Observation, index: int) -> Node | None:
    """Step from a matched node to the node actually wanted.

    None relation means the match is the target. NEXT_SIBLING is index + 1 —
    see the document order contract at the top of this module.
    """
    if relation is None:
        return observation.nodes[index]
    if relation is Relation.NEXT_SIBLING:
        following = index + 1
        if following >= len(observation.nodes):
            return None
        return observation.nodes[following]
    # Exhaustive over Relation. Adding a member without handling it here is a
    # type error rather than a silent fall-through to the wrong node.
    assert_never(relation)


def _near(a: Rect, b: Rect) -> bool:
    """Whether two boxes describe the same place on screen."""
    return (
        abs((a.x + a.width / 2) - (b.x + b.width / 2)) <= GEOMETRY_TOLERANCE_PX
        and abs((a.y + a.height / 2) - (b.y + b.height / 2)) <= GEOMETRY_TOLERANCE_PX
    )
