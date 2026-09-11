"""A serializable snapshot of the surface at one instant.

Plain data with no I/O and no vendor types, which is what makes ScriptedSurface
possible: an Observation written to JSON and read back is indistinguishable from
one captured live.

Deliberately absent: CSS selectors, XPath, HTML, and any driver object. How an
element is addressed is an accident of the technology that rendered the page and
does not belong in the domain.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Rect:
    """Element geometry in viewport coordinates."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class NodeRef:
    """An opaque, observation-scoped handle to one control.

    `observation_id` is carried so a ref cannot be silently reused against a
    later Observation; the surface keeps the private map from ref to the real
    element, and the domain treats `value` as a meaningless string.
    """

    observation_id: str
    value: str


@dataclass(frozen=True)
class Node:
    """One control, normalised so the same shape could come from a desktop
    accessibility tree rather than a browser.

    `destination` is the route a control leads to, when the surface can know it
    ahead of time — an anchor's target, a form's action. It exists so policy can
    refuse an action *before* it happens: a route allowlist checked only after
    navigation cannot undo an irreversible side effect, and a click is not a
    navigation, so the destination has to travel with the control.

    It is a route, normalised exactly as url_pattern is, never a raw href.
    Identifiers must not enter an Observation, and an anchor to a member page
    would otherwise smuggle one in. None means the surface cannot tell.
    """

    ref: NodeRef
    role: str
    name: str | None
    text: str | None
    frame_id: str
    bounds: Rect
    enabled: bool
    visible: bool
    destination: str | None = None


@dataclass(frozen=True)
class Observation:
    """The surface at one instant, as the system sees it.

    `url_pattern` is a route, never a full URL: member identifiers must not
    enter an Observation, because Observations are written to evidence.
    """

    observation_id: str
    nodes: tuple[Node, ...]
    url_pattern: str
    page_title: str
    captured_at: datetime

    def all_text(self) -> str:
        parts: list[str] = [self.page_title]
        parts += [n.text for n in self.nodes if n.text]
        parts += [n.name for n in self.nodes if n.name]
        return " ".join(parts).lower()
