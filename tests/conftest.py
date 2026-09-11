"""Shared test helpers.

The captured screens are loaded by ScriptedSurface, because reading JSON off
disk is that adapter's mechanism. The fixtures here are thin wrappers over it.
"""

from pathlib import Path

import pytest

from cua.adapters.scripted_surface import load_observation as _load
from cua.domain.observation import Observation

FIXTURES = Path(__file__).parent / "fixtures" / "observations"


def load_observation(name: str) -> Observation:
    return _load(FIXTURES / f"{name}.json")


@pytest.fixture
def search_page() -> Observation:
    """The dashboard: one control named Find, two named Search."""
    return load_observation("search_page")


@pytest.fixture
def search_page_filled() -> Observation:
    """The dashboard with a member number typed in, before submitting."""
    return load_observation("search_page_filled")


@pytest.fixture
def search_page_filled_unknown() -> Observation:
    """The dashboard with a member number that matches nobody, before submitting."""
    return load_observation("search_page_filled_unknown")


@pytest.fixture
def search_results() -> Observation:
    """The dashboard after a successful search: the row is a link to the member."""
    return load_observation("search_results")


@pytest.fixture
def search_page_filled_restricted() -> Observation:
    """The dashboard with a restricted member's number typed in."""
    return load_observation("search_page_filled_restricted")


@pytest.fixture
def search_results_restricted() -> Observation:
    """The result grid for a member the operator may not open."""
    return load_observation("search_results_restricted")


@pytest.fixture
def member_detail() -> Observation:
    """A member page: thirteen rowheaders, each followed by its value cell."""
    return load_observation("member_detail")


@pytest.fixture
def not_found() -> Observation:
    """The dashboard after searching for a member that does not exist."""
    return load_observation("not_found")


@pytest.fixture
def session_expired() -> Observation:
    """The sign on page, served where a member page was asked for."""
    return load_observation("session_expired")


@pytest.fixture
def member_denied() -> Observation:
    """The denial page for a membership flagged for restricted handling."""
    return load_observation("member_denied")


@pytest.fixture
def interstitial() -> Observation:
    """A maintenance notice standing in front of the page that was requested."""
    return load_observation("interstitial")
