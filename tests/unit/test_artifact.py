"""Artifact documents are translated into the complete replay domain model."""

from copy import deepcopy
from typing import Any

import pytest

from cua.domain.actions import ActionType, Effect
from cua.domain.artifact import capability_from_document
from cua.domain.capability import Approval, Confidence, Relation, SignalKind
from cua.domain.conditions import DetectorKind, RecoveryAction
from cua.domain.errors import MalformedArtifact
from cua.domain.outcomes import Outcome
from cua.domain.policy import Sensitivity


def minimal_document() -> dict[str, Any]:
    return {
        "contract": {
            "id": "lookup_member",
            "version": "1.2.0",
            "effect": "read_only",
        }
    }


def test_minimal_document_uses_safe_authoring_defaults():
    capability = capability_from_document(minimal_document())

    assert capability.contract.name == "lookup_member"
    assert capability.contract.version == "1.2.0"
    assert capability.contract.effect is Effect.READ_ONLY
    assert capability.contract.approval is Approval.DRAFT
    assert capability.contract.goal == ""
    assert capability.contract.preconditions == ()
    assert capability.contract.inputs == ()
    assert capability.contract.outputs == ()
    assert capability.contract.provenance is None
    assert capability.steps == ()
    assert capability.conditions == ()
    assert capability.success == ()


def test_complete_document_maps_authoring_vocabulary_to_domain_types():
    document = {
        "contract": {
            "id": "lookup_member",
            "version": "2.1.3",
            "description": "  Read a member record.  ",
            "effect": "read_only",
            "approval": "approved",
            "preconditions": ["authenticated_session"],
            "provenance": {
                "source": "discovery",
                "author": "operator-7",
                "recorded_against": {
                    "app": "backoffice",
                    "release": "4.2.11",
                    "variant": "regional",
                },
                "note": "  verified against a capture  ",
            },
            "inputs": [
                {
                    "name": "member_id",
                    "type": "string",
                    "sensitivity": "personal",
                    "required": False,
                    "pattern": "[0-9]{6}",
                    "description": "  Statement number  ",
                }
            ],
            "outputs": [
                {
                    "name": "balance",
                    "type": "decimal",
                    "sensitivity": "internal",
                    "transform": "strip_whitespace",
                    "description": "  Current balance  ",
                }
            ],
        },
        "steps": [
            {
                "id": "enter_member",
                "action": "type",
                "value_template": "{{ inputs.member_id }}",
                "target": {
                    "intent": "  member number field  ",
                    "rationale": "  unique label  ",
                    "signals": [
                        {
                            "kind": "role_name",
                            "confidence": "high",
                            "role": "textbox",
                            "name": "Member Number",
                        },
                        {
                            "kind": "anchor",
                            "confidence": "medium",
                            "role": "rowheader",
                            "text": "Member Number",
                            "relation": "next_sibling",
                        },
                        {
                            "kind": "geometry",
                            "confidence": "low",
                            "bounds": {"x": 1, "y": 2, "width": 3, "height": 4},
                            "brittle": True,
                        },
                    ],
                },
                "checkpoint": {
                    "detectors": [
                        {
                            "kind": "field_value_equals",
                            "target_ref": "self",
                            "value": "{{ inputs.member_id }}",
                        }
                    ]
                },
            }
        ],
        "conditions": [
            {
                "name": "maintenance",
                "outcome": "recoverable",
                "detail": "  notice is in the way  ",
                "code": "MAINTENANCE",
                "resume_checkpoint": "notice_gone",
                "detectors": [{"kind": "text_present", "text": "System Notice"}],
                "recovery": {
                    "action": "dismiss",
                    "max_attempts": 3,
                    "wait_ms": 250,
                    "target": {
                        "intent": "Continue",
                        "signals": [
                            {
                                "kind": "role_name",
                                "role": "button",
                                "name": "Continue",
                            }
                        ],
                    },
                },
            }
        ],
        "success": {
            "detectors": [{"kind": "url_pattern_is", "pattern": "/members/{member_id}"}]
        },
    }

    capability = capability_from_document(document)

    contract = capability.contract
    assert contract.goal == "Read a member record."
    assert contract.approval is Approval.APPROVED
    assert contract.preconditions == ("authenticated_session",)
    assert contract.inputs[0].sensitivity is Sensitivity.PERSONAL
    assert not contract.inputs[0].required
    assert contract.inputs[0].pattern == "[0-9]{6}"
    assert contract.outputs[0].transform == "strip_whitespace"
    assert contract.provenance is not None
    assert contract.provenance.release == "4.2.11"
    assert contract.provenance.note == "verified against a capture"

    step = capability.steps[0]
    assert step.action_type is ActionType.TYPE
    assert step.target is not None
    assert step.target.intent == "member number field"
    assert [signal.kind for signal in step.target.signals] == [
        SignalKind.ROLE_NAME,
        SignalKind.ANCHOR,
        SignalKind.GEOMETRY,
    ]
    assert step.target.signals[0].confidence is Confidence.HIGH
    assert step.target.signals[1].relation is Relation.NEXT_SIBLING
    assert step.target.signals[2].bounds is not None
    assert step.target.signals[2].bounds.width == 3.0
    assert step.target.signals[2].brittle
    assert step.checkpoint[0].kind is DetectorKind.FIELD_VALUE_EQUALS

    condition = capability.conditions[0]
    assert condition.outcome is Outcome.RECOVERABLE
    assert condition.detail == "notice is in the way"
    assert condition.recovery is not None
    assert condition.recovery.action is RecoveryAction.DISMISS
    assert condition.recovery.max_attempts == 3
    assert condition.recovery.target is not None
    assert capability.success[0].url_pattern == "/members/{member_id}"


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "Find"), ("text", "Member Number"), ("selector", "#member")],
)
def test_signal_value_aliases_are_normalized_to_name(field, value):
    document = minimal_document()
    document["steps"] = [
        {
            "id": "act",
            "action": "click",
            "target": {
                "signals": [{"kind": "role_name", field: value}],
            },
        }
    ]

    capability = capability_from_document(document)

    assert capability.steps[0].target is not None
    assert capability.steps[0].target.signals[0].name == value


@pytest.mark.parametrize(
    ("path", "bad_value", "message"),
    [
        (("contract", "effect"), "observe_only", "unknown effect"),
        (("contract", "approval"), "reviewed", "unknown approval"),
    ],
)
def test_unknown_contract_enums_name_the_invalid_field(path, bad_value, message):
    document = minimal_document()
    current = document
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = bad_value

    with pytest.raises(MalformedArtifact, match=message):
        capability_from_document(document)


def test_unknown_action_is_rejected_before_replay():
    document = minimal_document()
    document["steps"] = [{"id": "act", "action": "double_click"}]

    with pytest.raises(MalformedArtifact, match="unknown action"):
        capability_from_document(document)


def test_unknown_detector_fields_are_rejected_instead_of_ignored():
    document = minimal_document()
    document["success"] = {
        "detectors": [{"kind": "title_is", "title": "Member", "typo": "ignored?"}]
    }

    with pytest.raises(MalformedArtifact, match="unknown field.*typo"):
        capability_from_document(document)


@pytest.mark.parametrize("missing", ["id", "version", "effect"])
def test_required_contract_keys_report_the_missing_name(missing):
    document = minimal_document()
    copied = deepcopy(document)
    del copied["contract"][missing]

    with pytest.raises(MalformedArtifact, match=missing):
        capability_from_document(copied)
