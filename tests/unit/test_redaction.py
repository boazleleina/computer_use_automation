"""A sensitive value must not reach the evidence stream.

Driven by sensitivity declared on a capability's inputs and outputs, never by
scanning recorded text. A pattern matching "password" matches the field label
while the value typed into it goes straight through, and a scan publishes
whatever it fails to recognise. A declared type fails closed instead.

The assertion that matters is the last one: the raw value does not appear
anywhere in what the sink received, at any depth, in any field.
"""

import json
from collections.abc import Mapping

from cua.domain.policy import (
    RedactionRules,
    Rendering,
    Sensitivity,
    mask,
    redact_event,
)
from cua.ports.evidence import EvidenceSink

RULES = RedactionRules(
    secret=Rendering.DROP,
    personal=Rendering.MASK,
    internal=Rendering.RECORD,
    unclassified=Rendering.DROP,
)

MEMBER_NUMBER = "100045"
PASSWORD = "pa55w0rd-do-not-log"

DECLARED = {
    "member_id": Sensitivity.PERSONAL,
    "operator_password": Sensitivity.SECRET,
    "step": Sensitivity.INTERNAL,
    "url_pattern": Sensitivity.INTERNAL,
}


class FakeEvidenceSink:
    """Records what it was handed. Satisfies EvidenceSink structurally."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.blobs: dict[str, bytes] = {}

    def append(self, run_id: str, event: Mapping[str, object]) -> None:
        self.events.append(dict(event))

    def attach(self, run_id: str, name: str, payload: bytes) -> str:
        self.blobs[name] = payload
        return f"blob://{name}"

    def close(self) -> None:
        return None


def wire(sink: EvidenceSink) -> EvidenceSink:
    """Typed slot. mypy proves FakeEvidenceSink satisfies the port here."""
    return sink


def test_a_secret_is_dropped_but_the_field_survives():
    """The key stays, the value does not.

    Removing the key entirely would make the record lie: it would read as though
    no password was ever entered.
    """
    redacted = redact_event({"operator_password": PASSWORD}, DECLARED, RULES)

    assert "operator_password" in redacted
    assert redacted["operator_password"] is None


def test_personal_data_is_masked_not_dropped():
    """A run record has to stay debuggable. The last four digits are enough to
    tell two runs apart and not enough to identify a member."""
    redacted = redact_event({"member_id": MEMBER_NUMBER}, DECLARED, RULES)

    assert redacted["member_id"] == "****0045"
    assert MEMBER_NUMBER not in str(redacted["member_id"])


def test_internal_values_are_recorded_as_they_are():
    redacted = redact_event({"url_pattern": "/members/{member_id}"}, DECLARED, RULES)
    assert redacted["url_pattern"] == "/members/{member_id}"


def test_an_undeclared_field_fails_closed():
    """The property a scan cannot have.

    A scan's default is to publish what it does not recognise. This defaults to
    dropping it, so forgetting to classify a field costs you evidence rather
    than a member's data.
    """
    redacted = redact_event({"something_nobody_typed": MEMBER_NUMBER}, DECLARED, RULES)

    assert redacted["something_nobody_typed"] is None


def test_masking_short_values_reveals_nothing():
    """A four digit value masked to its last four is not masked at all."""
    assert mask("12") == "****"
    assert mask("1234") == "****"
    assert mask("12345") == "****2345"


def test_nested_values_are_redacted_too():
    """Evidence records are not flat. A value hidden one level down is still a
    value that reaches disk."""
    event = {
        "step": 3,
        "inputs": {"member_id": MEMBER_NUMBER, "operator_password": PASSWORD},
        "history": [{"member_id": MEMBER_NUMBER}],
    }
    redacted = redact_event(event, DECLARED, RULES)

    serialized = json.dumps(redacted)
    assert MEMBER_NUMBER not in serialized
    assert PASSWORD not in serialized


def test_the_declared_values_never_reach_the_sink():
    """Checked against the sink rather than a return value.

    Redaction happens once, at the boundary, so what a caller handed over is not
    what lands. Scanning the serialized stream is the same check as grepping the
    evidence directory for a member number.
    """
    sink = FakeEvidenceSink()
    wire(sink)  # mypy proves the fake satisfies the port here

    raw = {
        "step": 1,
        "url_pattern": "/members/{member_id}",
        "inputs": {"member_id": MEMBER_NUMBER, "operator_password": PASSWORD},
    }
    sink.append("run_1", redact_event(raw, DECLARED, RULES))

    stream = json.dumps(sink.events)
    assert MEMBER_NUMBER not in stream
    assert PASSWORD not in stream
    assert "/members/{member_id}" in stream  # internal data survives, or evidence is useless


def test_a_declared_value_copied_into_another_field_is_caught():
    """Rendering keys off the field name and is blind to the same value
    elsewhere. url_pattern is declared internal, so nothing about the field says
    it should be withheld — but this one is carrying a member number.
    """
    event = {
        "url_pattern": "/members/100045",
        "inputs": {"member_id": MEMBER_NUMBER},
    }
    redacted = redact_event(event, DECLARED, RULES)

    assert MEMBER_NUMBER not in json.dumps(redacted)
    assert redacted["url_pattern"] is None


def test_a_secret_quoted_in_an_error_message_is_caught():
    """The realistic version: an application echoes what was typed."""
    event = {
        "step": f"sign on failed for value {PASSWORD}",
        "inputs": {"operator_password": PASSWORD},
    }
    redacted = redact_event(event, DECLARED, RULES)

    assert PASSWORD not in json.dumps(redacted)
    assert redacted["step"] is None


def test_the_backstop_leaves_unrelated_internal_data_alone():
    """It drops fields carrying a declared value, not every field."""
    event = {
        "url_pattern": "/members/{member_id}",
        "step": "read the savings balance",
        "inputs": {"member_id": MEMBER_NUMBER},
    }
    redacted = redact_event(event, DECLARED, RULES)

    assert redacted["url_pattern"] == "/members/{member_id}"
    assert redacted["step"] == "read the savings balance"
    assert redacted["inputs"] == {"member_id": "****0045"}


def test_redaction_does_not_mutate_what_it_was_given():
    """A caller must not be able to tell whether its record was redacted by
    looking at the object it still holds."""
    original = {"member_id": MEMBER_NUMBER}
    redact_event(original, DECLARED, RULES)
    assert original["member_id"] == MEMBER_NUMBER


def test_rules_can_be_tightened_without_touching_call_sites():
    """personal: mask is a config choice, not a property of the code."""
    strict = RedactionRules(
        secret=Rendering.DROP,
        personal=Rendering.DROP,
        internal=Rendering.RECORD,
        unclassified=Rendering.DROP,
    )
    redacted = redact_event({"member_id": MEMBER_NUMBER}, DECLARED, strict)
    assert redacted["member_id"] is None


def test_an_absent_value_is_not_masked_into_looking_present():
    """Nothing to hide is not the same as something hidden.

    A click carries no value. Masking the absence of one wrote "****" into the
    record, which reads as a value that was entered and withheld — the same
    kind of lie that dropping the key instead of the value would tell, pointed
    the other way.
    """
    redacted = redact_event({"member_id": None}, DECLARED, RULES)

    assert "member_id" in redacted
    assert redacted["member_id"] is None
