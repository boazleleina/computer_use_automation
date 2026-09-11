"""Shared test helpers.

Loads the captured Observation fixtures.

The domain does not know what JSON is. ScriptedSurface reads JSON off disk
because that is its mechanism, the same way a browser surface reads an
accessibility tree because that is its; both hand back an Observation, and the
domain type stays a plain frozen dataclass with nothing to deserialise it.

So this parsing lives here until ScriptedSurface exists, which is the thing that
actually needs it. Then it moves into that adapter and the fixtures below become
thin wrappers around it.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from cua.domain.observation import Node, NodeRef, Observation, Rect

FIXTURES = Path(__file__).parent / "fixtures" / "observations"


def load_observation(name: str) -> Observation:
    """Read a captured fixture into domain types."""
    document = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return Observation(
        observation_id=document["observation_id"],
        nodes=tuple(
            Node(
                ref=NodeRef(**node["ref"]),
                role=node["role"],
                name=node["name"],
                text=node["text"],
                frame_id=node["frame_id"],
                bounds=Rect(**node["bounds"]),
                enabled=node["enabled"],
                visible=node["visible"],
                destination=node["destination"],
            )
            for node in document["nodes"]
        ),
        url_pattern=document["url_pattern"],
        page_title=document["page_title"],
        captured_at=datetime.fromisoformat(document["captured_at"]),
    )


@pytest.fixture
def search_page() -> Observation:
    """The dashboard: one control named Find, two named Search."""
    return load_observation("search_page")


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
    """The login page, served where a member page was asked for."""
    return load_observation("session_expired")
