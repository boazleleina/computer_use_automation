"""How a screen becomes an Observation.

Shared by every surface that perceives a real browser, and by the capture tool
that produced the recorded fixtures. One copy on purpose: if a scripted run and
a live run normalised differently, the scripted tests would pass against
behaviour the browser does not reproduce, and the fixtures would be describing
an application that does not exist.

Everything here is decided about the accessibility tree, not about Playwright.
It takes plain dictionaries in the shape Chrome reports and hands back domain
types, so a different driver that can produce the same tree needs none of it
rewritten.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from cua.domain.observation import Node, NodeRef, Rect

# Roles that describe the page's plumbing rather than anything on it.
SKIP_ROLES: Final = frozenset(
    {
        "generic",
        "none",
        "InlineTextBox",
        "GenericContainer",
        "RootWebArea",
        "LineBreak",
        "",
        # Always repeats the name of the cell that contains it.
        "StaticText",
        # Chrome's marker for a table used as layout. This application is built
        # from nested layout tables, so these are pure noise, and anything worth
        # keeping inside one also appears as a real cell.
        "LayoutTable",
        "LayoutTableRow",
        "LayoutTableCell",
    }
)

# Structural nodes with no accessible name identify nothing. Form controls are
# kept regardless, because an unnamed control is itself worth seeing.
INTERACTIVE_ROLES: Final = frozenset(
    {"button", "textbox", "combobox", "checkbox", "radio", "link", "listbox"}
)

MAIN_FRAME: Final = "main"

# Anything four digits or longer in a path is an identifier, not a route.
IDENTIFIER_SEGMENT: Final = re.compile(r"/\d{4,}")
ORIGIN: Final = re.compile(r"^https?://[^/]+")

ZERO_BOUNDS: Final = Rect(x=0.0, y=0.0, width=0.0, height=0.0)


def route_pattern(url: str) -> str:
    """A url reduced to the route it names.

    Identifiers must not enter an Observation, because an Observation is
    written to evidence and a route with a member number in it puts that
    number in a file. It is also what lets one capability serve every member:
    /members/100045 and /members/100099 are the same state with different data.
    """
    path = ORIGIN.sub("", url) or "/"
    return IDENTIFIER_SEGMENT.sub("/{member_id}", path).split("?")[0]


def node_from_ax(
    entry: Mapping[str, Any],
    ref: NodeRef,
    frame_id: str,
    bounds: Rect,
    destination: str | None,
) -> Node:
    """One accessibility tree entry as a domain Node."""
    properties = {
        prop["name"]: (prop.get("value") or {}).get("value")
        for prop in entry.get("properties", [])
    }
    return Node(
        ref=ref,
        role=_value_of(entry.get("role")) or "",
        name=_value_of(entry.get("name")) or None,
        text=_value_of(entry.get("value")) or None,
        frame_id=frame_id,
        bounds=bounds,
        enabled=not bool(properties.get("disabled", False)),
        visible=bounds.width > 0 and bounds.height > 0,
        destination=destination,
    )


def is_worth_keeping(entry: Mapping[str, Any]) -> bool:
    """Whether this entry describes something on the screen.

    Ignored nodes, plumbing roles, and unnamed structure are dropped. An
    unnamed form control is kept: a control nobody labelled is a finding, not
    noise.
    """
    if entry.get("ignored"):
        return False
    role = _value_of(entry.get("role")) or ""
    if role in SKIP_ROLES:
        return False
    return _value_of(entry.get("name")) is not None or role in INTERACTIVE_ROLES


def surviving_indices(nodes: Sequence[Node]) -> list[int]:
    """Which nodes survive filtering, by their position before it.

    Positions rather than nodes, because a surface has to carry something
    alongside each node — the element it names — and filtering the two lists
    separately is how a ref ends up pointing at the wrong control.
    """
    heading_names = {n.name for n in nodes if n.role == "heading" and n.name}
    return [
        index
        for index, node in enumerate(nodes)
        if not (node.role == "cell" and node.name in heading_names)
    ]


def without_heading_duplicates(nodes: Sequence[Node]) -> list[Node]:
    """Drop cells that only repeat a heading.

    A panel head is a table cell wrapping a heading, so both arrive with the
    same accessible name. The heading is the one worth targeting and the cell
    carries nothing extra.
    """
    return [nodes[index] for index in surviving_indices(nodes)]


def renumber(nodes: Sequence[Node], observation_id: str) -> list[Node]:
    """Give the surviving nodes contiguous refs.

    Gaps would read as nodes that went missing when the screen was captured
    rather than ones that were filtered afterwards.
    """
    return [
        _with_ref(node, NodeRef(observation_id=observation_id, value=f"{node.frame_id}:{index}"))
        for index, node in enumerate(nodes)
    ]


def _with_ref(node: Node, ref: NodeRef) -> Node:
    from dataclasses import replace

    return replace(node, ref=ref)


def _value_of(field: Mapping[str, Any] | None) -> str | None:
    if not field:
        return None
    value = field.get("value")
    return str(value) if value not in (None, "") else None
