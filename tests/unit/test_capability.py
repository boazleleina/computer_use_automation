"""The artifact: what a capability takes, how it works, what the screens mean.

Three layers, because they answer different questions. The contract is what
somebody approves. The steps are what runs. The conditions are how the
application's replies get interpreted.
"""

import pytest

from cua.domain.actions import ActionType, Effect
from cua.domain.capability import (
    Capability,
    Confidence,
    Contract,
    InputSpec,
    OutputSpec,
    Relation,
    Signal,
    SignalKind,
    Step,
    TargetSpec,
)
from cua.domain.conditions import Condition, Detector, DetectorKind
from cua.domain.errors import UnsafeCapability
from cua.domain.outcomes import Outcome, Result
from cua.domain.policy import Sensitivity

MEMBER_FOUND = Condition(
    name="MEMBER_FOUND",
    outcome=Outcome.SUCCESS,
    detectors=(
        Detector(kind=DetectorKind.URL_PATTERN, url_pattern="/members/{member_id}"),
        Detector(kind=DetectorKind.NODE_PRESENT, role="heading", name="Member Detail"),
    ),
    detail="the member detail page rendered",
)

NOT_FOUND = Condition(
    name="MEMBER_NOT_FOUND",
    outcome=Outcome.BUSINESS_OUTCOME,
    detectors=(Detector(kind=DetectorKind.TEXT_PRESENT, text="No member matches"),),
    detail="the search returned no member",
)

LOOKUP = Capability(
    contract=Contract(
        name="lookup_member_balance",
        version=1,
        goal="read a member's savings balance",
        effect=Effect.READ_ONLY,
        inputs=(InputSpec(name="member_id", type="string", sensitivity=Sensitivity.PERSONAL),),
        outputs=(
            OutputSpec(name="savings_balance", type="decimal", sensitivity=Sensitivity.PERSONAL),
        ),
    ),
    steps=(
        Step(
            index=0,
            action_type=ActionType.TYPE,
            target=TargetSpec(
                intent="the member number field",
                rationale="labelled control, unique on the page",
                signals=(
                    Signal(kind=SignalKind.ROLE_NAME, confidence=Confidence.HIGH,
                           role="textbox", name="Member Number"),
                ),
            ),
            value="{{ inputs.member_id }}",
        ),
        Step(
            index=1,
            action_type=ActionType.CLICK,
            target=TargetSpec(
                intent="the enquiry button",
                rationale="one control named Find on this page",
                signals=(
                    Signal(kind=SignalKind.ROLE_NAME, confidence=Confidence.HIGH,
                           role="button", name="Find"),
                ),
            ),
            checkpoint=MEMBER_FOUND,
        ),
        Step(
            index=2,
            action_type=ActionType.READ,
            target=TargetSpec(
                intent="the savings balance value",
                rationale="the value cell is named by its own content, so anchor on the header",
                signals=(
                    Signal(kind=SignalKind.ANCHOR, confidence=Confidence.HIGH,
                           role="rowheader", name="Savings Balance",
                           relation=Relation.NEXT_SIBLING),
                ),
            ),
            reads_into="savings_balance",
        ),
    ),
    conditions=(MEMBER_FOUND, NOT_FOUND),
)


def test_the_contract_declares_what_the_capability_takes_and_returns():
    assert LOOKUP.contract.name == "lookup_member_balance"
    assert LOOKUP.contract.version == 1
    assert LOOKUP.contract.effect is Effect.READ_ONLY
    assert [spec.name for spec in LOOKUP.contract.inputs] == ["member_id"]
    assert [spec.name for spec in LOOKUP.contract.outputs] == ["savings_balance"]


def test_sensitivity_is_declared_once_and_read_from_the_contract():
    """The map redaction runs on comes from here, not from a list kept beside it.

    Two copies of this would drift, and the copy that drifts is the one that
    stops covering a field.
    """
    assert LOOKUP.declared_sensitivity() == {
        "member_id": Sensitivity.PERSONAL,
        "savings_balance": Sensitivity.PERSONAL,
    }


def test_the_artifact_holds_no_member_data():
    """The value is a placeholder bound at replay time.

    A capability compiled with 100045 baked into it would be one member's
    procedure rather than the flow's, and would carry that member's number into
    every copy of the artifact.
    """
    typing_step = LOOKUP.step(0)
    assert typing_step.value == "{{ inputs.member_id }}"

    written = repr(LOOKUP)
    assert "100045" not in written


def test_a_step_is_addressed_by_its_declared_index():
    assert LOOKUP.step(1).action_type is ActionType.CLICK
    with pytest.raises(KeyError):
        LOOKUP.step(99)


def test_the_step_that_reads_names_the_output_it_fills():
    """Otherwise a capability declares an output nothing produces."""
    read_step = LOOKUP.step(2)
    assert read_step.action_type is ActionType.READ
    assert read_step.reads_into == "savings_balance"
    assert read_step.reads_into in {spec.name for spec in LOOKUP.contract.outputs}


def test_a_step_carries_the_checkpoint_that_must_hold_after_it():
    """Without one, a step that silently did nothing looks like a step that
    worked."""
    assert LOOKUP.step(1).checkpoint is MEMBER_FOUND
    assert LOOKUP.step(0).checkpoint is None


def test_conditions_cover_more_than_the_happy_path():
    """A capability that only recognises success treats every other screen as
    unknown, which escalates to a person for a member that does not exist."""
    outcomes = {condition.outcome for condition in LOOKUP.conditions}
    assert Outcome.SUCCESS in outcomes
    assert Outcome.BUSINESS_OUTCOME in outcomes


def test_a_capability_cannot_declare_a_password_input():
    """The contract is where a credential would enter the automated path.

    A secret input is not a malformed artifact. It parses, it validates, and it
    runs: the compiler parameterises it, replay binds it, and a step types it.
    Refusing it at construction is what makes "replay never holds a credential"
    a property of the type rather than a rule nobody happened to break.
    """
    with pytest.raises(UnsafeCapability) as raised:
        Contract(
            name="sign_on",
            version=1,
            goal="sign the operator in",
            effect=Effect.READ_ONLY,
            inputs=(
                InputSpec(name="operator_password", type="string",
                          sensitivity=Sensitivity.SECRET),
            ),
        )

    assert "operator_password" in str(raised.value)
    assert "handover" in str(raised.value)


def test_a_capability_cannot_return_a_secret_either():
    """The same rule from the other side.

    Redaction would drop it on the way to evidence, so the capability would be
    declaring an output it can never hand back — and it would have held the
    value to get that far.
    """
    with pytest.raises(UnsafeCapability):
        Contract(
            name="fetch_token",
            version=1,
            goal="return a session token",
            effect=Effect.READ_ONLY,
            outputs=(
                OutputSpec(name="session_token", type="string",
                           sensitivity=Sensitivity.SECRET),
            ),
        )


@pytest.mark.parametrize("sensitivity", [Sensitivity.PERSONAL, Sensitivity.INTERNAL])
def test_personal_and_internal_fields_are_permitted(sensitivity):
    """The rule is about credentials, not about caution generally. A capability
    that cannot take a member number cannot do anything."""
    contract = Contract(
        name="lookup",
        version=1,
        goal="read a balance",
        effect=Effect.READ_ONLY,
        inputs=(InputSpec(name="member_id", type="string", sensitivity=sensitivity),),
    )
    assert contract.inputs[0].sensitivity is sensitivity


def test_a_result_reports_the_outcome_and_the_declared_outputs():
    result = Result(
        run_id="run_1",
        capability=LOOKUP.contract.name,
        version=LOOKUP.contract.version,
        outcome=Outcome.SUCCESS,
        detail="the member detail page rendered",
        condition_name="MEMBER_FOUND",
        outputs={"savings_balance": "4820.55"},
        step_index=2,
        resolved_via=SignalKind.ANCHOR,
    )

    assert result.ok
    assert result.outputs["savings_balance"] == "4820.55"
    assert result.resolved_via is SignalKind.ANCHOR


def test_a_business_outcome_is_neither_ok_nor_a_failure():
    """`ok` means the capability did what it was asked. A member that does not
    exist is an answer, so callers that care read `outcome` instead."""
    result = Result(
        run_id="run_1",
        capability=LOOKUP.contract.name,
        version=1,
        outcome=Outcome.BUSINESS_OUTCOME,
        detail="the search returned no member",
        condition_name="MEMBER_NOT_FOUND",
    )

    assert not result.ok
    assert result.outcome is Outcome.BUSINESS_OUTCOME
    assert result.outputs == {}


def test_a_failed_result_says_what_it_expected_and_what_it_found():
    """"Step 4 failed" is not something anyone can act on."""
    result = Result(
        run_id="run_1",
        capability=LOOKUP.contract.name,
        version=1,
        outcome=Outcome.INTERVENTION_REQUIRED,
        detail="the session aged out mid run",
        condition_name="LOGIN_REQUIRED",
        step_index=1,
        expected="heading 'Member Detail'",
        observed="page title 'Sign On - Riverside CU Back Office'",
        evidence_ref="blob://step-1-screenshot.png",
    )

    assert not result.ok
    assert result.expected and result.observed
    assert result.evidence_ref
