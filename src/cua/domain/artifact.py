"""Turning an artifact document into a Capability.

The document is a plain mapping by the time it gets here. Reading YAML off disk
is a store's mechanism and lives in an adapter; deciding what counts as a valid
capability is not, and lives here, so that no store implementation can quietly
become the authority on that.

Two vocabularies meet in this file. The document uses the words a person writes
when authoring an artifact — `id`, `description`, `value_template`, `pattern` —
and the domain uses the words the code reasons in. Mapping between them once,
in one place, is what lets the document read naturally without the domain
carrying an author's shorthand around.
"""

from collections.abc import Mapping, Sequence
from typing import Any

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
from cua.domain.conditions import Condition, Detector, DetectorKind, Recovery, RecoveryAction
from cua.domain.errors import MalformedArtifact
from cua.domain.outcomes import Outcome
from cua.domain.policy import Sensitivity

# Where a signal keeps the thing it matches on. The document names the field
# after what the author is writing — a selector, a label, a control name — and
# they all land in Signal.name.
SIGNAL_VALUE_KEYS = ("name", "text", "selector")

# The document says `pattern`, the detector field is `url_pattern`.
DETECTOR_KEY_ALIASES = {"pattern": "url_pattern"}


def capability_from_document(document: Mapping[str, Any]) -> Capability:
    """Build a capability from an artifact document.

    Raises `MalformedArtifact` for missing required fields, unknown enum
    values, or unknown detector fields. Capability safety rules may also raise
    `UnsafeCapability` during construction.
    """
    return Capability(
        contract=_contract(_require(document, "contract")),
        steps=tuple(_step(s) for s in document.get("steps", ())),
        conditions=tuple(_condition(c) for c in document.get("conditions", ())),
        success=_detectors(document.get("success", {}).get("detectors", ())),
    )


def _contract(document: Mapping[str, Any]) -> Contract:
    return Contract(
        name=str(_require(document, "id")),
        version=str(_require(document, "version")),
        goal=str(document.get("description", "")).strip(),
        effect=_enum(Effect, _require(document, "effect"), "effect"),
        approval=_enum(Approval, document.get("approval", "draft"), "approval"),
        preconditions=tuple(str(p) for p in document.get("preconditions", ())),
        inputs=tuple(_input(i) for i in document.get("inputs", ())),
        outputs=tuple(_output(o) for o in document.get("outputs", ())),
        provenance=_provenance(document.get("provenance")),
    )


def _provenance(document: Mapping[str, Any] | None) -> Provenance | None:
    if not document:
        return None
    recorded = document.get("recorded_against", {}) or {}
    return Provenance(
        source=str(document.get("source", "")),
        author=str(document.get("author", "")),
        app=str(recorded.get("app", "")),
        release=str(recorded.get("release", "")),
        variant=str(recorded.get("variant", "")),
        note=str(document.get("note", "")).strip(),
    )


def _input(document: Mapping[str, Any]) -> InputSpec:
    return InputSpec(
        name=str(_require(document, "name")),
        type=str(document.get("type", "string")),
        sensitivity=_enum(Sensitivity, document.get("sensitivity", "internal"), "sensitivity"),
        required=bool(document.get("required", True)),
        pattern=_optional_str(document.get("pattern")),
        description=str(document.get("description", "")).strip(),
    )


def _output(document: Mapping[str, Any]) -> OutputSpec:
    return OutputSpec(
        name=str(_require(document, "name")),
        type=str(document.get("type", "string")),
        sensitivity=_enum(Sensitivity, document.get("sensitivity", "internal"), "sensitivity"),
        transform=_optional_str(document.get("transform")),
        description=str(document.get("description", "")).strip(),
    )


def _step(document: Mapping[str, Any]) -> Step:
    checkpoint = document.get("checkpoint", {}) or {}
    return Step(
        id=str(_require(document, "id")),
        action_type=_enum(ActionType, _require(document, "action"), "action"),
        target=_target(document.get("target")),
        value=_optional_str(document.get("value_template")),
        checkpoint=_detectors(checkpoint.get("detectors", ())),
        reads_into=_optional_str(document.get("reads_into")),
    )


def _target(document: Mapping[str, Any] | None) -> TargetSpec | None:
    if not document:
        return None
    return TargetSpec(
        intent=str(document.get("intent", "")).strip(),
        rationale=str(document.get("rationale", "")).strip(),
        signals=tuple(_signal(s) for s in document.get("signals", ())),
    )


def _signal(document: Mapping[str, Any]) -> Signal:
    value = next((document[k] for k in SIGNAL_VALUE_KEYS if k in document), None)
    bounds = document.get("bounds")
    return Signal(
        kind=_enum(SignalKind, _require(document, "kind"), "signal kind"),
        confidence=_enum(Confidence, document.get("confidence", "medium"), "confidence"),
        role=_optional_str(document.get("role")),
        name=_optional_str(value),
        relation=(
            _enum(Relation, document["relation"], "relation") if "relation" in document else None
        ),
        bounds=_rect(bounds) if bounds else None,
        brittle=bool(document.get("brittle", False)),
    )


def _rect(document: Mapping[str, Any]) -> Any:
    from cua.domain.observation import Rect

    return Rect(
        x=float(document["x"]),
        y=float(document["y"]),
        width=float(document["width"]),
        height=float(document["height"]),
    )


def _condition(document: Mapping[str, Any]) -> Condition:
    return Condition(
        name=str(_require(document, "name")),
        outcome=_enum(Outcome, _require(document, "outcome"), "outcome"),
        detectors=_detectors(document.get("detectors", ())),
        detail=str(document.get("detail", "")).strip(),
        code=_optional_str(document.get("code")),
        resume_checkpoint=_optional_str(document.get("resume_checkpoint")),
        recovery=_recovery(document.get("recovery")),
    )


def _recovery(document: Mapping[str, Any] | None) -> Recovery | None:
    if not document:
        return None
    return Recovery(
        action=_enum(RecoveryAction, _require(document, "action"), "recovery action"),
        max_attempts=int(document.get("max_attempts", 2)),
        wait_ms=int(document.get("wait_ms", 1000)),
        target=_target(document.get("target")),
    )


def _detectors(documents: Sequence[Mapping[str, Any]]) -> tuple[Detector, ...]:
    return tuple(_detector(d) for d in documents)


def _detector(document: Mapping[str, Any]) -> Detector:
    """Build a detector, rejecting fields outside the detector vocabulary."""
    fields = {DETECTOR_KEY_ALIASES.get(k, k): v for k, v in document.items() if k != "kind"}
    known = set(Detector.__dataclass_fields__) - {"kind"}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise MalformedArtifact(f"detector has unknown field(s) {unknown}")
    return Detector(
        kind=_enum(DetectorKind, _require(document, "kind"), "detector kind"),
        **{k: _optional_str(v) for k, v in fields.items()},
    )


def _require(document: Mapping[str, Any], key: str) -> Any:
    """Return a required document value or raise `MalformedArtifact`."""
    if key not in document:
        raise MalformedArtifact(f"missing required key {key!r}")
    return document[key]


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _enum(enum_type: Any, value: Any, label: str) -> Any:
    """Look a value up in a closed set, and name the alternatives when it fails.

    A document naming an action nobody implements is an authoring mistake, and
    it should say so here rather than resolving to nothing half way through a
    run against a live application.
    """
    try:
        return enum_type(value)
    except ValueError:
        allowed = sorted(m.value for m in enum_type)
        raise MalformedArtifact(f"unknown {label} {value!r}; expected one of {allowed}") from None
