"""Which control is this?

Getting it wrong means typing an account number into the wrong member's field,
so the cases that matter here are the ones where resolution declines to answer
rather than the ones where it succeeds.
"""

from datetime import UTC, datetime

import pytest

from cua.domain.capability import Confidence, Relation, Signal, SignalKind, TargetSpec
from cua.domain.observation import Node, NodeRef, Observation, Rect
from cua.domain.resolution import Ambiguous, NotFound, Resolved, resolve

ALL_KINDS = frozenset(SignalKind)
PORTABLE_KINDS = frozenset(SignalKind) - {SignalKind.WEB_CSS}


def spec(*signals: Signal, intent: str = "test target") -> TargetSpec:
    return TargetSpec(intent=intent, rationale="written by a test", signals=signals)


def node(role: str, name: str | None, index: int = 0) -> Node:
    return Node(
        ref=NodeRef(observation_id="obs_test", value=f"main:{index}"),
        role=role,
        name=name,
        text=None,
        frame_id="main",
        bounds=Rect(x=0.0, y=0.0, width=10.0, height=10.0),
        enabled=True,
        visible=True,
    )


def observation(*nodes: Node) -> Observation:
    return Observation(
        observation_id="obs_test",
        nodes=nodes,
        url_pattern="/test",
        page_title="Test",
        captured_at=datetime.now(UTC),
    )


def test_unique_role_name_resolves(search_page):
    """The happy path. One control named Find, so there is one answer."""
    result = resolve(
        spec(Signal(kind=SignalKind.ROLE_NAME, confidence=Confidence.HIGH,
                    role="button", name="Find")),
        search_page,
        ALL_KINDS,
    )
    assert isinstance(result, Resolved)
    assert result.via_signal is SignalKind.ROLE_NAME

    matched = next(n for n in search_page.nodes if n.ref == result.ref)
    assert (matched.role, matched.name) == ("button", "Find")


def test_two_matches_returns_ambiguous_and_chooses_nothing(search_page):
    """Two controls named Search. Guessing is how you act on the wrong account.

    The assertion that matters is not that Ambiguous is returned, but that no
    ref comes back with it: there is nothing for a caller to accidentally use.
    """
    result = resolve(
        spec(Signal(kind=SignalKind.ROLE_NAME, confidence=Confidence.MEDIUM,
                    role="button", name="Search")),
        search_page,
        ALL_KINDS,
    )
    assert isinstance(result, Ambiguous)
    assert result.count == 2
    assert result.at_signal is SignalKind.ROLE_NAME
    assert not hasattr(result, "ref")


def test_falls_through_to_a_lower_signal_and_records_which_one(search_page):
    """The top signal is absent, the next one matches. via_signal is drift telemetry:
    a capability that starts resolving on a weaker signal is a capability about to break.
    """
    result = resolve(
        spec(
            Signal(kind=SignalKind.ROLE_NAME, confidence=Confidence.HIGH,
                   role="button", name="Retrieve Member"),
            Signal(kind=SignalKind.ROLE_NAME, confidence=Confidence.MEDIUM,
                   role="button", name="Find"),
        ),
        search_page,
        ALL_KINDS,
    )
    assert isinstance(result, Resolved)
    assert result.via_signal is SignalKind.ROLE_NAME
    assert result.signal_index == 1


def test_unsupported_signal_kind_is_skipped_and_the_skip_is_recorded(search_page):
    """web.css has no meaning on a desktop surface.

    An unsupported signal is stepped over rather than failed on, and the step is
    recorded, so a capability carrying a signal this surface cannot honour is
    visible in the run record rather than silently one signal weaker.
    """
    result = resolve(
        spec(
            Signal(kind=SignalKind.WEB_CSS, confidence=Confidence.HIGH,
                   role="button", name="#ctl00_cph_btnFind"),
            Signal(kind=SignalKind.ROLE_NAME, confidence=Confidence.HIGH,
                   role="button", name="Find"),
        ),
        search_page,
        PORTABLE_KINDS,
    )
    assert isinstance(result, Resolved)
    assert result.via_signal is SignalKind.ROLE_NAME
    assert result.skipped == (SignalKind.WEB_CSS,)


def test_no_signal_matches_returns_not_found(search_page):
    result = resolve(
        spec(Signal(kind=SignalKind.ROLE_NAME, confidence=Confidence.HIGH,
                    role="button", name="Delete Member")),
        search_page,
        ALL_KINDS,
    )
    assert isinstance(result, NotFound)


def test_anchor_next_sibling_targets_the_value_cell(member_detail):
    """A value cell's accessible name is its own content.

    Chrome names a cell from what is in it; headers= associates the row header
    for announcement without changing the name. So the balance cell is named
    "4820.55" — the number we are trying to read — and cannot be targeted by
    name. Anchoring on the rowheader and stepping one node is the only way.
    """
    result = resolve(
        spec(Signal(kind=SignalKind.ANCHOR, confidence=Confidence.HIGH,
                    role="rowheader", name="Savings Balance",
                    relation=Relation.NEXT_SIBLING)),
        member_detail,
        ALL_KINDS,
    )
    assert isinstance(result, Resolved)
    assert result.via_signal is SignalKind.ANCHOR

    matched = next(n for n in member_detail.nodes if n.ref == result.ref)
    assert matched.role == "cell"
    assert matched.name == "4820.55"


def test_anchor_with_no_following_node_is_not_found():
    """The anchor is the last node. Stepping past the end is NotFound, not a crash."""
    result = resolve(
        spec(Signal(kind=SignalKind.ANCHOR, confidence=Confidence.HIGH,
                    role="rowheader", name="Savings Balance",
                    relation=Relation.NEXT_SIBLING)),
        observation(node("rowheader", "Savings Balance")),
        ALL_KINDS,
    )
    assert isinstance(result, NotFound)


def test_ambiguous_anchor_does_not_resolve():
    """Two rowheaders with the same name means two candidate values.

    Ambiguity at the anchor is ambiguity at the target: stepping from either one
    would produce a different number, and picking the first is guessing.
    """
    result = resolve(
        spec(Signal(kind=SignalKind.ANCHOR, confidence=Confidence.HIGH,
                    role="rowheader", name="Balance",
                    relation=Relation.NEXT_SIBLING)),
        observation(
            node("rowheader", "Balance", 0),
            node("cell", "1.00", 1),
            node("rowheader", "Balance", 2),
            node("cell", "2.00", 3),
        ),
        ALL_KINDS,
    )
    assert isinstance(result, Ambiguous)
    assert result.count == 2


def test_label_matches_by_name_when_the_role_has_changed():
    """LABEL ignores role on purpose.

    A field re-rendered as a dropdown keeps its label and loses its role. That
    is the case this kind exists for, and it is why it sits above geometry:
    the label is what a person would use to find the control again.
    """
    obs = observation(node("combobox", "Account Type"))
    result = resolve(
        spec(
            Signal(kind=SignalKind.ROLE_NAME, confidence=Confidence.HIGH,
                   role="textbox", name="Account Type"),
            Signal(kind=SignalKind.LABEL, confidence=Confidence.MEDIUM, name="Account Type"),
        ),
        obs,
        ALL_KINDS,
    )
    assert isinstance(result, Resolved)
    assert result.via_signal is SignalKind.LABEL
    assert result.signal_index == 1


def test_geometry_resolves_a_node_at_the_same_place():
    """Last resort, and only with brittle metadata set."""
    target = Node(
        ref=NodeRef(observation_id="obs_test", value="main:1"),
        role="button", name=None, text=None, frame_id="main",
        bounds=Rect(x=100.0, y=200.0, width=70.0, height=19.0),
        enabled=True, visible=True,
    )
    result = resolve(
        spec(Signal(kind=SignalKind.GEOMETRY, confidence=Confidence.LOW,
                    bounds=Rect(x=101.0, y=201.0, width=70.0, height=19.0), brittle=True)),
        observation(node("heading", "Somewhere else", 0), target),
        ALL_KINDS,
    )
    assert isinstance(result, Resolved)
    assert result.ref == target.ref
    assert result.via_signal is SignalKind.GEOMETRY


def test_geometry_outside_tolerance_does_not_match():
    """A control that moved is a different control, not the same one nearby."""
    result = resolve(
        spec(Signal(kind=SignalKind.GEOMETRY, confidence=Confidence.LOW,
                    bounds=Rect(x=0.0, y=0.0, width=10.0, height=10.0), brittle=True)),
        observation(node("button", "Find", 0)),  # sits at 0,0 but 10x10 vs a far box
        ALL_KINDS,
    )
    assert isinstance(result, Resolved)  # same place, so this one does match

    far = resolve(
        spec(Signal(kind=SignalKind.GEOMETRY, confidence=Confidence.LOW,
                    bounds=Rect(x=900.0, y=900.0, width=10.0, height=10.0), brittle=True)),
        observation(node("button", "Find", 0)),
        ALL_KINDS,
    )
    assert isinstance(far, NotFound)


def test_geometry_without_bounds_matches_nothing():
    """A geometry signal carrying no geometry is malformed, not a wildcard."""
    result = resolve(
        spec(Signal(kind=SignalKind.GEOMETRY, confidence=Confidence.LOW)),
        observation(node("button", "Find")),
        ALL_KINDS,
    )
    assert isinstance(result, NotFound)


def test_web_css_is_skipped_even_when_the_surface_claims_to_support_it():
    """The domain cannot evaluate a selector, whatever the surface says.

    Matching web.css needs a selector engine, and the domain holds no selectors
    by design. So the escape hatch is carried in the artifact and stepped over
    here, rather than quietly resolving to nothing.
    """
    result = resolve(
        spec(
            Signal(kind=SignalKind.WEB_CSS, confidence=Confidence.HIGH,
                   name="#ctl00_cph_btnFind"),
            Signal(kind=SignalKind.ROLE_NAME, confidence=Confidence.HIGH,
                   role="button", name="Find"),
        ),
        observation(node("button", "Find")),
        ALL_KINDS,  # web.css included, and still skipped
    )
    assert isinstance(result, Resolved)
    assert result.skipped == (SignalKind.WEB_CSS,)


@pytest.mark.parametrize("fixture_name", ["search_page", "member_detail", "not_found"])
def test_fixtures_are_in_document_order(fixture_name, request):
    """resolve() implements NEXT_SIBLING as index + 1.

    That is only correct while Observation.nodes preserves document order, which
    no type can enforce. This test pins the assumption to the captured fixtures
    so a future surface that reorders nodes fails here rather than silently
    reading the wrong cell.
    """
    obs = request.getfixturevalue(fixture_name)
    assert [n.ref.value for n in obs.nodes] == [f"main:{i}" for i in range(len(obs.nodes))]
