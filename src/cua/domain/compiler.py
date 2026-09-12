"""Turning a run that worked into a capability that can be repeated.

The hinge of the whole system. Discovery produces a trajectory — this member,
this screen, this afternoon — and replay needs a procedure that works for every
member on every afternoon. Everything here is the work of getting from one to
the other, and every rule in it is a claim about what was incidental to that
run and what was essential to the task.

Two decisions govern the rest.

Literals are parameterised from bindings the caller supplies, never guessed.
The alternative is a heuristic that decides six digits look like a member
number, which is wrong the first time a capability types a six digit amount
into a field. What was a parameter is something the person who ran discovery
knows and the trajectory does not record.

Targets are rebuilt from what the control is, not from where it was. The node
the run touched is on a screen that no longer exists; its role and accessible
name are the part that will still be true next release. A value cell named by
its own contents cannot be targeted that way at all — it is found through the
row header beside it, which is why ANCHOR exists.

Pure. No I/O, no clock, no randomness. Given the same trajectory and bindings
this produces the same capability, which matters because the artifact is the
thing a human approves and an approved artifact must not change under them.
"""

import re
from collections.abc import Mapping, Sequence

from cua.domain.actions import ActionType, Effect
from cua.domain.capability import (
    Approval,
    Capability,
    Confidence,
    Contract,
    InputSpec,
    OutputSpec,
    Provenance,
    Relation,
    Signal,
    SignalKind,
    Step,
    TargetSpec,
)
from cua.domain.conditions import Condition, Detector, DetectorKind
from cua.domain.errors import MalformedArtifact
from cua.domain.observation import Node, Observation
from cua.domain.outcomes import Outcome
from cua.domain.policy import Sensitivity
from cua.domain.trajectory import ExecutedStep, Trajectory

# Roles that label the cell after them. A value cell carries no name of its own
# worth targeting, so the header beside it is the only stable handle.
HEADER_ROLES = frozenset({"rowheader", "columnheader"})

NON_WORD = re.compile(r"[^a-z0-9]+")


def compile_capability(
    trajectory: Trajectory,
    name: str,
    version: str,
    inputs: Mapping[str, str],
    *,
    app: str = "unknown",
    release: str = "unknown",
    author: str = "discovery",
) -> Capability:
    """One successful trajectory as a reviewable, replayable capability.

    Refuses a trajectory that did not reach its goal. Compiling a run that
    stopped would produce a capability that confidently performs most of a
    task, and most of a task is the worst outcome available: it leaves the
    application half way through a flow with a caller that was told nothing.
    """
    if not trajectory.succeeded:
        raise MalformedArtifact(
            f"this run stopped on {trajectory.stopped_because.value} and cannot be "
            "compiled; only a run that reached its goal describes a repeatable flow"
        )
    if not trajectory.steps:
        raise MalformedArtifact("a capability with no steps does nothing")

    # An empty literal matches between every pair of characters, so str.replace
    # would scatter the placeholder through every description in the artifact.
    empty = sorted(name for name, literal in inputs.items() if not literal)
    if empty:
        raise MalformedArtifact(
            f"input(s) {empty} were bound to an empty value, which names no literal "
            "in the recorded run and cannot be parameterised out of it"
        )

    kept = _without_repeated_reads(trajectory.steps)
    steps = tuple(_step(executed, index, inputs) for index, executed in enumerate(kept))
    outputs = tuple(
        _output(executed) for executed in kept if executed.read_value is not None
    )

    contract = Contract(
        name=name,
        version=version,
        goal=_describe(trajectory.goal, inputs),
        effect=_effect(trajectory),
        # Discovered, therefore draft. A capability a model wrote is exactly
        # the kind that must not run unattended until somebody has read it,
        # and defaulting the other way would make review optional in practice.
        approval=Approval.DRAFT,
        preconditions=(),
        inputs=tuple(_input(input_name, literal) for input_name, literal in inputs.items()),
        outputs=outputs,
        provenance=Provenance(
            source="discovered",
            author=author,
            app=app,
            release=release,
            variant="base",
            note=(
                "Compiled from a discovery trajectory. The rationales in the "
                "run log are the model's reasoning at the time; the targets "
                "below are this compiler's, rebuilt from what each control was."
            ),
        ),
    )

    return Capability(
        contract=contract,
        steps=steps,
        conditions=_conditions(trajectory, inputs),
        success=_success(trajectory.steps[0].before, trajectory.steps[-1].after, inputs),
    )


def _without_repeated_reads(steps: Sequence[ExecutedStep]) -> tuple[ExecutedStep, ...]:
    """Drop reads of a value the run has already taken.

    A model that cannot see what a read returned will ask for the same cell
    again — the loop is bounded by max_steps, so the run still ends, but the
    trajectory carries the repetition. Compiling it produced five steps reading
    one balance into five outputs with the same name, which Contract refuses
    outright and which would be nonsense if it did not.

    Only reads, and only repeats. Clicking the same button twice can be a real
    part of a flow; reading the same cell twice cannot mean anything different
    the second time.
    """
    seen: set[tuple[str, str | None]] = set()
    kept: list[ExecutedStep] = []
    for step in steps:
        if step.read_value is not None and step.node is not None:
            # Keyed on the control, not on the name derived from it. Two
            # different cells with no row header both name themselves "value",
            # and collapsing those would drop a genuine second output.
            cell = (step.node.ref.value, step.node.name)
            if cell in seen:
                continue
            seen.add(cell)
        kept.append(step)
    return tuple(kept)


def _conditions(trajectory: Trajectory, inputs: Mapping[str, str]) -> tuple[Condition, ...]:
    """One condition per screen the run legitimately passed through.

    Not optional, and not padding. The replay engine treats a screen no
    condition describes as INTERVENTION_REQUIRED, which is the right default —
    an unrecognised screen is somebody's to look at — but it means an artifact
    carrying no conditions cannot execute a single step. A compiler that left
    them out would emit artifacts that look complete and stop on contact.

    What is here is only the happy path, because that is all one successful run
    saw. The conditions that matter most — not found, permission denied,
    session expired — cannot be derived from a run that met none of them, and a
    person adds those. That is a large part of what reviewing a discovered
    artifact is for.
    """
    visited = [trajectory.steps[0].before] + [step.after for step in trajectory.steps]

    # Headings on every screen are the application's furniture, not a name for
    # any one of them. This page banner sits across the top of all of them.
    banner: set[str | None] = set.intersection(
        *({n.name for n in screen.nodes if n.role == "heading"} for screen in visited)
    )

    conditions: dict[tuple[str, str | None], Condition] = {}
    for screen in visited:
        heading = next(
            (
                n
                for n in screen.nodes
                if n.role == "heading" and _nameable(n.name) and n.name not in banner
            ),
            None,
        )
        key = (screen.url_pattern, heading.name if heading else None)
        if key in conditions:
            continue

        detectors = [Detector(kind=DetectorKind.URL_PATTERN, url_pattern=screen.url_pattern)]
        if heading is not None:
            detectors.append(
                Detector(
                    kind=DetectorKind.NODE_PRESENT,
                    role=heading.role,
                    name=_parameterise(heading.name, inputs),
                )
            )

        # Parameterised before it becomes a name, for the same reason as the
        # detector: a heading reading "Member 100045" would otherwise name a
        # condition after one member and never match another.
        landmark = _describe(heading.name, inputs) if heading and heading.name else None
        name = _slug(landmark) if landmark else _slug(screen.url_pattern)
        conditions[key] = Condition(
            name=f"{name}_ready",
            outcome=Outcome.SUCCESS,
            detectors=tuple(detectors),
            detail=_describe(f"{screen.page_title} at {screen.url_pattern}", inputs),
        )

    return tuple(conditions.values())


# ---- steps -----------------------------------------------------------------


def _step(executed: ExecutedStep, index: int, inputs: Mapping[str, str]) -> Step:
    action_type = executed.proposal.action_type
    if action_type is None:  # pragma: no cover - the loop cannot execute one
        raise MalformedArtifact(f"step {index} executed without an action type")

    target = None if executed.node is None else _target(executed, inputs)
    return Step(
        id=_describe(_step_id(executed, index, action_type), inputs),
        action_type=action_type,
        target=target,
        value=_parameterise(executed.proposal.value, inputs),
        checkpoint=_checkpoint(executed, action_type, inputs),
        reads_into=_output_name(executed) if executed.read_value is not None else None,
    )


def _step_id(executed: ExecutedStep, index: int, action_type: ActionType) -> str:
    """A name a person can read, and a number so two cannot collide.

    Derived from the control rather than invented, so a diff between two
    recordings of the same flow lines up step by step.

    A value cell is named by its own contents, so naming the step after the
    control would put the data in the step id: 04_read_4820_55 and
    05_read_test_member_one are a member's balance and their name, written into
    a file that gets committed, reviewed and shared. The row header beside it
    says what the step is for without saying whose it is, and reads better
    anyway — 04_read_savings_balance is the name somebody would have chosen.
    """
    node = executed.node
    if node is None:
        subject = "screen"
    else:
        anchor = _anchor_for(node, executed.before)
        named = anchor.name if anchor is not None and anchor.name else node.name
        subject = _slug(named) if named else "screen"
    return f"{index + 1:02d}_{action_type.value}_{subject}"


def _target(executed: ExecutedStep, inputs: Mapping[str, str]) -> TargetSpec:
    """How to find this control again.

    Ranked signals, strongest first, exactly as a hand written artifact would
    carry them. No web.css: the domain cannot evaluate a selector, and emitting
    one would put a signal in the artifact that resolve() must always skip.
    """
    node = executed.node
    if node is None:  # pragma: no cover - guarded by the caller
        raise MalformedArtifact("a target was asked for where no control was touched")

    anchor = _anchor_for(node, executed.before)
    if anchor is not None:
        return TargetSpec(
            intent=_describe(f"Value beside the {anchor.name!r} row header", inputs),
            rationale=(
                "The cell is named by its own contents, so it cannot be found by "
                "name without already knowing the answer. The stable fact is the "
                "adjacency: the header names the row and the value follows it."
            ),
            signals=(
                Signal(
                    kind=SignalKind.ANCHOR,
                    confidence=Confidence.HIGH,
                    role=anchor.role,
                    name=anchor.name,
                    relation=Relation.NEXT_SIBLING,
                ),
            ),
        )

    return TargetSpec(
        intent=_describe(
            f"{node.role} {node.name!r}" if node.name else f"unnamed {node.role}", inputs
        ),
        rationale=(
            "Role and accessible name. Both come from the accessibility tree "
            "rather than from markup, which is what survives a framework "
            "regenerating its ids."
        ),
        signals=(
            Signal(
                kind=SignalKind.ROLE_NAME,
                confidence=Confidence.HIGH,
                role=node.role,
                name=_parameterise(node.name, inputs),
            ),
        ),
    )


def _anchor_for(node: Node, observation: Observation) -> Node | None:
    """The header this cell belongs to, if it is a value cell at all.

    Document order is the contract Observation makes, so "the node before it"
    is a real relationship and not a guess about layout.

    A cell preceded by a row header, and nothing finer than that. The tempting
    extra test — that the cell's accessible name is its own displayed value —
    does not work: a cell carries its content as its name and reports no text
    at all, so that check rejects every value cell on the screen. The adjacency
    is the whole signal, and it is the one the application actually guarantees.
    """
    if node.role != "cell":
        return None

    nodes = observation.nodes
    position = next((i for i, n in enumerate(nodes) if n.ref == node.ref), None)
    if position is None or position == 0:
        return None

    previous = nodes[position - 1]
    if previous.role in HEADER_ROLES and previous.name:
        return previous
    return None


def _checkpoint(
    executed: ExecutedStep, action_type: ActionType, inputs: Mapping[str, str]
) -> tuple[Detector, ...]:
    """What has to be true for this step to have worked.

    Taken from the screen the action actually produced, never from what it was
    supposed to produce. A checkpoint written from an expectation asserts the
    author's belief; one written from the observation asserts what the
    application did.
    """
    if action_type is ActionType.READ:
        # A read changes nothing, so there is no "what is new" to assert. What
        # is worth asserting is that the row it read from is there: if the
        # header has moved or gone, the cell beside it is somebody else's
        # value and the output would be wrong rather than missing.
        node = executed.node
        anchor = _anchor_for(node, executed.before) if node is not None else None
        landmark = anchor or node
        if landmark is None or not landmark.name:  # pragma: no cover - reads have targets
            return ()
        return (
            Detector(
                kind=DetectorKind.NODE_PRESENT,
                role=landmark.role,
                name=_parameterise(landmark.name, inputs),
            ),
        )

    if action_type is ActionType.TYPE:
        # The field holds what was typed. Cheap, exact, and it catches the
        # case where a control silently refused the input.
        return (
            Detector(
                kind=DetectorKind.FIELD_VALUE_EQUALS,
                target_ref="self",
                value=_parameterise(executed.proposal.value, inputs),
            ),
        )

    after = executed.after
    detectors: list[Detector] = []
    if after.url_pattern != executed.before.url_pattern:
        detectors.append(Detector(kind=DetectorKind.URL_PATTERN, url_pattern=after.url_pattern))

    landmark = _landmark(executed)
    if landmark is not None:
        detectors.append(
            Detector(
                kind=DetectorKind.NODE_PRESENT,
                role=landmark.role,
                name=_parameterise(landmark.name, inputs),
            )
        )

    if not detectors:
        # Nothing distinguishes the new screen from the old one. Saying so is
        # better than inventing an assertion that would pass on both.
        raise MalformedArtifact(
            f"the {action_type.value} step produced a screen with nothing new on it, "
            "so there is no honest checkpoint to write"
        )
    return tuple(detectors)


def _landmark(executed: ExecutedStep) -> Node | None:
    """Something on the new screen that was not on the old one.

    A heading first: headings name a screen, which is what a checkpoint wants
    to assert. Anything named will do otherwise.
    """
    before = {(n.role, n.name) for n in executed.before.nodes}
    fresh = [
        node
        for node in executed.after.nodes
        if _nameable(node.name) and (node.role, node.name) not in before
    ]
    if not fresh:
        return None
    return next((n for n in fresh if n.role == "heading"), fresh[0])


def _nameable(name: str | None) -> bool:
    """Whether a name is worth asserting on.

    Layout tables in this application produce cells holding a single
    non-breaking space, and one of those was the first "new" node on the result
    screen. A checkpoint asserting the presence of a blank cell is not wrong so
    much as meaningless: it would pass on almost any page.
    """
    return bool(name and name.strip())


# ---- the contract ----------------------------------------------------------


def _input(name: str, literal: str) -> InputSpec:
    """One parameter, typed from the literal it replaced.

    personal by default, because this is a bank and the things a capability is
    parameterised by are member numbers and account references. A reviewer can
    widen it; nobody has to remember to narrow it.
    """
    return InputSpec(
        name=name,
        type="string",
        sensitivity=Sensitivity.PERSONAL,
        required=True,
        pattern=_pattern_for(literal),
        description=f"Supplied per invocation. Recorded against {_shape(literal)}.",
    )


def _pattern_for(literal: str) -> str | None:
    """A pattern the recorded value satisfies, where its shape is obvious.

    Only digits, and only the exact length. A looser guess would reject valid
    inputs on a flow nobody has tested, and a cleverer one would be a guess
    about the institution's numbering rather than about this artifact.
    """
    if literal.isdigit():
        return f"^[0-9]{{{len(literal)}}}$"
    return None


def _shape(literal: str) -> str:
    return f"a {len(literal)} digit value" if literal.isdigit() else "a text value"


def _output(executed: ExecutedStep) -> OutputSpec:
    return OutputSpec(
        name=_output_name(executed),
        type="string",
        sensitivity=Sensitivity.PERSONAL,
        transform="strip_whitespace",
        description="Read from the member detail screen.",
    )


def _output_name(executed: ExecutedStep) -> str:
    """What to call the value this step read.

    Named after the header beside it, because that is what the screen calls it
    and a reviewer comparing artifact to application should not have to
    translate. Falls back to the position when there is no header to borrow.
    """
    node = executed.node
    if node is not None:
        anchor = _anchor_for(node, executed.before)
        if anchor is not None and anchor.name:
            return _slug(anchor.name)
        if node.name:
            return _slug(node.name)
    return "value"


def _effect(trajectory: Trajectory) -> Effect:
    """What this capability does to the data behind the screen.

    Read only unless something was typed, selected or submitted. Conservative
    in the direction that matters: a capability wrongly marked read_only would
    be allowed to run unattended by may_run_unattended, so the doubt goes the
    other way.
    """
    touched = {
        step.proposal.action_type
        for step in trajectory.steps
        if step.proposal.action_type is not None
    }
    if touched <= {ActionType.READ, ActionType.NAVIGATE}:
        return Effect.READ_ONLY
    return Effect.MUTATING


def _success(
    first: Observation, final: Observation, inputs: Mapping[str, str]
) -> tuple[Detector, ...]:
    """Where the capability has to end up.

    Route and a heading, both. Route alone is satisfied by an expired session,
    which renders a sign-on form at the route of the page it replaced and
    returns 200 while doing it.

    The heading has to be one that was not already on the screen the run
    started from. The first heading on this application's pages is the banner
    across the top of every one of them, and a success condition satisfied by
    the banner is satisfied before the capability does anything.
    """
    detectors: list[Detector] = [
        Detector(kind=DetectorKind.URL_PATTERN, url_pattern=final.url_pattern)
    ]

    at_start = {n.name for n in first.nodes if n.role == "heading"}
    heading = next(
        (
            n
            for n in final.nodes
            if n.role == "heading" and _nameable(n.name) and n.name not in at_start
        ),
        None,
    )
    if heading is not None:
        detectors.append(
            Detector(
                kind=DetectorKind.NODE_PRESENT,
                role=heading.role,
                name=_parameterise(heading.name, inputs),
            )
        )
    return tuple(detectors)


# ---- parameterisation ------------------------------------------------------


def _parameterise(text: str | None, inputs: Mapping[str, str]) -> str | None:
    """Replace recorded literals with the inputs they came from.

    Whole value only, not a substring. Substituting inside a longer string
    would rewrite "Member 100045 Detail" into a template that no longer matches
    the heading it was taken from, and a checkpoint that cannot match is worse
    than one that is too specific.
    """
    if text is None:
        return None
    for name, literal in inputs.items():
        if text == literal:
            return f"{{{{ inputs.{name} }}}}"
    return text


def _describe(text: str, inputs: Mapping[str, str]) -> str:
    """The same substitution for prose, where a substring is safe to rewrite.

    Goals, step ids and target intents are read by people and matched against
    nothing, so replacing a member number inside one cannot break anything —
    while leaving it there puts that member number in a committed file. The
    rule is the opposite of _parameterise for exactly that reason: what makes
    substring substitution dangerous is matching, and none of these match.
    """
    for name, literal in inputs.items():
        text = text.replace(literal, f"{{{{ inputs.{name} }}}}")
    return text


def _slug(name: str) -> str:
    return NON_WORD.sub("_", name.strip().lower()).strip("_") or "value"


__all__ = ["compile_capability"]
