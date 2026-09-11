"""What just happened?

Getting this wrong means reporting "system error" when the honest answer was
"that member does not exist". The calling agent then retries or apologises
instead of answering, for a routine result.
"""

from datetime import UTC, datetime

from cua.domain.conditions import Condition, Detector, DetectorKind
from cua.domain.observation import Node, NodeRef, Observation, Rect
from cua.domain.outcomes import Outcome, classify

# A capability declares these. They live in the artifact as data, not code.
SUCCESS_ON_MEMBER_PAGE = Condition(
    name="MEMBER_FOUND",
    outcome=Outcome.SUCCESS,
    detectors=(
        Detector(kind=DetectorKind.URL_PATTERN, url_pattern="/members/{member_id}"),
        Detector(kind=DetectorKind.NODE_PRESENT, role="heading", name="Member Detail"),
    ),
    detail="the member detail page rendered",
)

# Deliberately naive: url only. It exists to prove severity precedence, because
# this is exactly the condition a careless author would write.
NAIVE_SUCCESS = Condition(
    name="NAIVE_MEMBER_FOUND",
    outcome=Outcome.SUCCESS,
    detectors=(Detector(kind=DetectorKind.URL_PATTERN, url_pattern="/members/{member_id}"),),
    detail="the url looks like a member page",
)

NOT_FOUND = Condition(
    name="MEMBER_NOT_FOUND",
    outcome=Outcome.BUSINESS_OUTCOME,
    detectors=(Detector(kind=DetectorKind.TEXT_PRESENT, text="No member matches"),),
    detail="the search returned no member",
)

LOGIN_REQUIRED = Condition(
    name="LOGIN_REQUIRED",
    outcome=Outcome.INTERVENTION_REQUIRED,
    detectors=(Detector(kind=DetectorKind.TEXT_PRESENT, text="Your session has ended"),),
    detail="the session aged out mid run",
)

PERMISSION_DENIED = Condition(
    name="PERMISSION_DENIED",
    outcome=Outcome.HARD_FAILURE,
    detectors=(
        Detector(kind=DetectorKind.TEXT_PRESENT, text="not authorised to view this membership"),
    ),
    detail="the operator may not view this member",
)

INTERSTITIAL = Condition(
    name="INTERSTITIAL",
    outcome=Outcome.RECOVERABLE,
    detectors=(Detector(kind=DetectorKind.TEXT_PRESENT, text="System Notice"),),
    detail="a maintenance interstitial is in the way",
)

ALL_CONDITIONS = (
    SUCCESS_ON_MEMBER_PAGE,
    NOT_FOUND,
    LOGIN_REQUIRED,
    PERMISSION_DENIED,
    INTERSTITIAL,
)


def page(*, url_pattern: str, title: str, texts: tuple[str, ...] = ()) -> Observation:
    return Observation(
        observation_id="obs_test",
        nodes=tuple(
            Node(
                ref=NodeRef(observation_id="obs_test", value=f"main:{i}"),
                role="cell", name=text, text=None, frame_id="main",
                bounds=Rect(x=0.0, y=0.0, width=10.0, height=10.0),
                enabled=True, visible=True,
            )
            for i, text in enumerate(texts)
        ),
        url_pattern=url_pattern,
        page_title=title,
        captured_at=datetime.now(UTC),
    )


def test_member_not_found_is_a_business_outcome_not_a_failure(not_found):
    """The run worked. The answer is "no such member". Classifying it as a failure
    would fire recovery, escalate to a human, and make the calling agent
    apologise instead of answering — for a routine result.
    """
    result = classify(not_found, ALL_CONDITIONS)

    assert result.outcome is Outcome.BUSINESS_OUTCOME
    assert result.condition_name == "MEMBER_NOT_FOUND"
    assert result.outcome not in {Outcome.HARD_FAILURE, Outcome.RECOVERABLE}


def test_session_expiry_asks_for_a_human(session_expired):
    result = classify(session_expired, ALL_CONDITIONS)

    assert result.outcome is Outcome.INTERVENTION_REQUIRED
    assert result.condition_name == "LOGIN_REQUIRED"


def test_the_worst_matching_outcome_wins(session_expired):
    """The trap this whole fixture exists for.

    The expired session renders the login page at the url of the member page it
    replaced, with HTTP 200. A success condition keyed on the route therefore
    matches, and so does LOGIN_REQUIRED. Believing the success one is how an
    automation reports a balance it never read.

    Precedence is by severity, not declaration order, so a careless author
    cannot make this dangerous by listing conditions in the wrong sequence.
    """
    assert session_expired.url_pattern == "/members/{member_id}"

    result = classify(session_expired, (NAIVE_SUCCESS, LOGIN_REQUIRED))
    assert result.outcome is Outcome.INTERVENTION_REQUIRED

    # Same two conditions, opposite order. Same answer.
    reversed_result = classify(session_expired, (LOGIN_REQUIRED, NAIVE_SUCCESS))
    assert reversed_result.outcome is Outcome.INTERVENTION_REQUIRED

    # Both are reported, so the too-weak success condition shows up in the record.
    assert set(result.matched) == {"NAIVE_MEMBER_FOUND", "LOGIN_REQUIRED"}


def test_the_member_page_classifies_as_success(member_detail):
    result = classify(member_detail, ALL_CONDITIONS)

    assert result.outcome is Outcome.SUCCESS
    assert result.condition_name == "MEMBER_FOUND"


def test_every_detector_in_a_condition_must_match(member_detail):
    """Detectors within a condition are ANDed.

    MEMBER_FOUND requires the route and the heading. The route alone is what
    the expired session also produces, so requiring both is the fix for the
    trap above at the level of a single condition.
    """
    route_only = page(url_pattern="/members/{member_id}", title="Sign On", texts=())
    result = classify(route_only, (SUCCESS_ON_MEMBER_PAGE,))

    assert result.condition_name is None

    both = classify(member_detail, (SUCCESS_ON_MEMBER_PAGE,))
    assert both.condition_name == "MEMBER_FOUND"


def test_an_unrecognised_screen_asks_for_a_human():
    """Fails safe.

    A screen no condition describes is not a success. The domain does not know
    what happened, so the only honest outcome is that somebody should look.
    """
    unknown = page(url_pattern="/somewhere", title="Unexpected", texts=("nothing familiar",))
    result = classify(unknown, ALL_CONDITIONS)

    assert result.condition_name is None
    assert result.outcome is Outcome.INTERVENTION_REQUIRED
    assert result.matched == ()


def test_permission_denied_is_a_hard_failure_not_a_business_outcome():
    """Different from not-found on purpose.

    "No such member" is an answer the caller wanted. "You may not look at this
    member" is the system refusing, and it needs a person to grant access.
    """
    denied = page(
        url_pattern="/members/{member_id}",
        title="Access Restricted",
        texts=("You are not authorised to view this membership.",),
    )
    result = classify(denied, ALL_CONDITIONS)

    assert result.outcome is Outcome.HARD_FAILURE
    assert result.condition_name == "PERMISSION_DENIED"


def test_an_interstitial_is_recoverable():
    """Nothing is wrong. Something is in the way, and waiting fixes it."""
    interstitial = page(
        url_pattern="/members/{member_id}",
        title="System Notice",
        texts=("A scheduled maintenance window is in progress on node CUBOS-03.",
               "System Notice"),
    )
    result = classify(interstitial, ALL_CONDITIONS)

    assert result.outcome is Outcome.RECOVERABLE
    assert result.condition_name == "INTERSTITIAL"


def test_classification_carries_the_declared_detail(not_found):
    """The artifact's own words, not a message the engine invented.

    This is what makes a run record readable by someone who was not there.
    """
    result = classify(not_found, ALL_CONDITIONS)
    assert result.detail == "the search returned no member"


def test_text_matching_ignores_case(not_found):
    """all_text() lowercases, so a condition author does not have to guess."""
    shouty = Condition(
        name="MEMBER_NOT_FOUND",
        outcome=Outcome.BUSINESS_OUTCOME,
        detectors=(Detector(kind=DetectorKind.TEXT_PRESENT, text="NO MEMBER MATCHES"),),
        detail="the search returned no member",
    )
    assert classify(not_found, (shouty,)).outcome is Outcome.BUSINESS_OUTCOME


def test_text_absent_detects_when_a_banner_has_gone(not_found):
    gone = Detector(kind=DetectorKind.TEXT_ABSENT, text="scheduled maintenance")
    present = Detector(kind=DetectorKind.TEXT_ABSENT, text="No member matches")

    assert gone.holds(not_found)
    assert not present.holds(not_found)


def test_title_detector_requires_an_exact_title(member_detail):
    exact = Detector(kind=DetectorKind.TITLE_IS, title=member_detail.page_title)
    different_case = Detector(kind=DetectorKind.TITLE_IS, title=member_detail.page_title.upper())

    assert exact.holds(member_detail)
    assert not different_case.holds(member_detail)


def test_field_value_detector_is_scoped_to_the_step_subject(search_page_filled):
    member_number = next(node for node in search_page_filled.nodes if node.name == "Member Number")
    detector = Detector(kind=DetectorKind.FIELD_VALUE_EQUALS, value="100045")

    assert detector.holds(search_page_filled, member_number.ref)
    assert not detector.holds(search_page_filled)

    other_field = next(node for node in search_page_filled.nodes if node.name == "From Date")
    assert not detector.holds(search_page_filled, other_field.ref)


def test_detector_missing_its_required_comparison_value_fails_closed(member_detail):
    detectors = (
        Detector(kind=DetectorKind.TEXT_PRESENT),
        Detector(kind=DetectorKind.TEXT_ABSENT),
        Detector(kind=DetectorKind.FIELD_VALUE_EQUALS),
    )

    assert not any(detector.holds(member_detail) for detector in detectors)
