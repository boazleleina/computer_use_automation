"""A surface that plays captured screens and records what was done to them.

The properties worth pinning are the ones a test would otherwise depend on by
accident: that observing is free, that acting moves the application on, and
that a handle to a replaced screen is refused here exactly as a browser would
refuse it.
"""

import pytest

from cua.adapters.scripted_surface import ActCall, ScriptedSurface
from cua.domain.actions import ActionType
from cua.domain.capability import SignalKind
from cua.domain.errors import SurfaceError
from cua.domain.observation import Node, NodeRef, Observation
from cua.ports.surface import Surface


def wire(surface: Surface) -> Surface:
    """Typed slot. mypy proves ScriptedSurface satisfies the port here."""
    return surface


def find(observation: Observation, role: str, name: str) -> Node:
    """The one node on this screen with that role and name."""
    return next(n for n in observation.nodes if n.role == role and n.name == name)


def value_beside(observation: Observation, header: str) -> Node:
    """The cell immediately after a row header, which is where its value sits."""
    anchor = find(observation, "rowheader", header)
    return observation.nodes[observation.nodes.index(anchor) + 1]


def test_observing_does_not_move_the_application_on(search_page, member_detail):
    """Called any number of times, the answer is the same screen.

    The engine observes a variable number of times per step: to classify, to
    check a checkpoint, again on every retry. None of that is the application
    doing anything, so none of it appears in a script.
    """
    surface = ScriptedSurface([search_page, member_detail])

    assert surface.observe() is search_page
    assert surface.observe() is search_page
    assert surface.observe() is search_page


def test_acting_moves_to_the_next_screen(search_page, member_detail):
    surface = ScriptedSurface([search_page, member_detail])
    button = find(search_page, "button", "Find")

    surface.act(ActionType.CLICK, button.ref)

    assert surface.observe() is member_detail


def test_reading_does_not_move_the_application_on(member_detail, not_found):
    """Two reads in a row both run against the page they were meant for."""
    surface = ScriptedSurface([member_detail, not_found])
    balance = value_beside(member_detail, "Savings Balance")

    assert surface.read(balance.ref) == "4820.55"
    assert surface.read(balance.ref) == "4820.55"
    assert surface.observe() is member_detail


def test_a_read_is_not_recorded_as_something_the_run_did(member_detail):
    """Reads observe, they do not mutate. `acted` is the record of actions."""
    surface = ScriptedSurface([member_detail])
    surface.read(value_beside(member_detail, "Account Name").ref)

    assert surface.acted == []


def test_read_uses_the_value_a_person_would_see(search_page_filled, member_detail):
    """An input carries its value in text, static content carries it in name.

    Both go through readable_value, so an artifact never mentions either.
    """
    filled = ScriptedSurface([search_page_filled])
    field = find(search_page_filled, "textbox", "Member Number")
    assert filled.read(field.ref) == "100045"

    detail = ScriptedSurface([member_detail])
    assert detail.read(value_beside(member_detail, "Savings Balance").ref) == "4820.55"


def test_a_stale_ref_is_refused(search_page, member_detail):
    """A handle to a page that has since been replaced is dead.

    Enforced here as a browser enforces it, so a test cannot pass while
    carrying a bug the real surface would raise on.
    """
    surface = ScriptedSurface([search_page, member_detail])
    button = find(search_page, "button", "Find")
    surface.act(ActionType.CLICK, button.ref)

    with pytest.raises(SurfaceError) as raised:
        surface.act(ActionType.CLICK, button.ref)

    assert "re-observe" in str(raised.value)


def test_a_stale_ref_is_refused_on_read_as_well(search_page, member_detail):
    surface = ScriptedSurface([search_page, member_detail])
    field = find(search_page, "textbox", "Member Number")
    surface.act(ActionType.TYPE, field.ref, "100045")

    with pytest.raises(SurfaceError):
        surface.read(field.ref)


def test_the_script_clamps_at_the_last_screen(search_page, member_detail):
    """Acting past the end of a script is not an IndexError from the fake.

    A test that acts once more than it meant to should fail on the assertion it
    was making, not on the plumbing underneath it.
    """
    surface = ScriptedSurface([search_page, member_detail])
    surface.act(ActionType.CLICK, find(search_page, "button", "Find").ref)
    surface.act(ActionType.CLICK, find(member_detail, "link", "Sign Off").ref)

    assert surface.observe() is member_detail
    assert len(surface.acted) == 2


def test_every_action_is_recorded_with_its_target_and_value(search_page):
    surface = ScriptedSurface([search_page])
    field = find(search_page, "textbox", "Member Number")

    surface.act(ActionType.TYPE, field.ref, "100045")

    assert surface.acted == [ActCall(ActionType.TYPE, field.ref, "100045")]


def test_nothing_acted_is_how_a_refusal_is_proven(search_page):
    """The assertion a policy refusal needs.

    A refused action is only demonstrably refused if the surface was never
    asked to perform it, and an empty record is what says so.
    """
    surface = ScriptedSurface([search_page])
    assert surface.acted == []


def test_read_is_not_accepted_as_an_action(search_page_filled):
    """Routing a read through act would record it as something the run did and
    advance the application under it."""
    surface = ScriptedSurface([search_page_filled])
    field = find(search_page_filled, "textbox", "Member Number")

    with pytest.raises(SurfaceError):
        surface.act(ActionType.READ, field.ref)


def test_web_css_is_not_offered_by_a_surface_with_no_selector_engine(search_page):
    surface = wire(ScriptedSurface([search_page]))

    supported = surface.supported_signal_kinds()

    assert SignalKind.WEB_CSS not in supported
    assert SignalKind.ROLE_NAME in supported
    assert SignalKind.ANCHOR in supported


def test_a_script_needs_at_least_one_screen():
    with pytest.raises(ValueError):
        ScriptedSurface([])


def test_reading_a_node_that_is_not_on_this_screen_is_refused(search_page, member_detail):
    """The ref is current, but it names nothing here."""
    surface = ScriptedSurface([search_page])
    ghost = NodeRef(observation_id=search_page.observation_id, value="main:999")

    with pytest.raises(SurfaceError):
        surface.read(ghost)


def test_screenshot_returns_something_evidence_can_attach(member_detail):
    surface = ScriptedSurface([member_detail])
    assert surface.screenshot()


def test_close_is_safe_to_call_twice(member_detail):
    surface = ScriptedSurface([member_detail])
    surface.close()
    surface.close()
