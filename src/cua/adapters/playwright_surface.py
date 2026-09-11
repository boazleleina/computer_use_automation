"""A surface that drives a real browser.

Perception comes from the accessibility tree, not from the markup. What the
browser reports a control to be is the same thing a person reading the screen
would say it is, and it is the only description that survives a framework
regenerating its ids on the next deploy.

Native handles never leave this module. A NodeRef is opaque; the map from ref
to the element it names is private and belongs to one observation. Anything
above this file works in domain types and could not reach a Playwright object
if it tried.

Resolution does not happen here. Locators like get_by_label fuse perceiving,
deciding and acting into one call, which would move the decision about which
control to touch out of the domain and into a driver, and take the ranked
signals and their telemetry with it. This surface reports what is on screen and
performs what it is told.

The browser is owned for the length of a run rather than a step, which is what
lets a person take the same live session during an escalation and hand it back.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Page, sync_playwright

from cua.adapters.observation_shape import (
    MAIN_FRAME,
    ZERO_BOUNDS,
    destination_pattern,
    is_worth_keeping,
    node_from_ax,
    route_pattern,
    surviving_indices,
)
from cua.domain.actions import ActionType
from cua.domain.capability import SignalKind
from cua.domain.errors import SurfaceError
from cua.domain.observation import Node, NodeRef, Observation, Rect, readable_value

# Stamped on each element an observation names, so an action can find the same
# element again through a locator. It is the automation's own handle rather than
# a test id the application ships: perception has already decided which control
# this is, from role and name, before anything is stamped.
REF_ATTRIBUTE = "data-cua-ref"

# web.css is excluded even though this surface could evaluate a selector.
# resolve() runs in the domain, which has no selector engine and never will, so
# a surface claiming to support it would be promising something the layer above
# cannot use.
PORTABLE_SIGNAL_KINDS = frozenset(SignalKind) - {SignalKind.WEB_CSS}

STAMP = "function (value) { this.setAttribute(arguments[1], value); }"

# Whatever the wrapped page call hands back, so the error boundary can sit in
# front of the reading operations as well as the acting ones.
T = TypeVar("T")


@dataclass
class PlaywrightSurface:
    """Perceive and act on a live page."""

    page: Page
    base_url: str
    action_timeout_ms: int = 5000

    # ref value -> the element that ref names, for this observation only.
    _handles: dict[str, int] = field(default_factory=dict, init=False)
    _current: Observation | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)

    def open(self, url: str) -> Observation:
        self._require_open()
        self._run(lambda: self.page.goto(self._absolute(url), timeout=self.action_timeout_ms))
        return self.observe()

    def observe(self) -> Observation:
        """Walk every frame and describe what is on screen.

        Each element an observation keeps is stamped with its own ref, so a
        later action finds the same element rather than one that merely looks
        like it. The stamp is rewritten on every observation, which is what
        makes a ref belong to exactly one of them.
        """
        self._require_open()
        return self._run(self._describe)

    def _describe(self) -> Observation:
        """The body of observe, inside the error boundary."""
        observation_id = f"obs_{uuid4().hex[:8]}"
        self._handles = {}

        seen: list[Node] = []
        elements: list[int | None] = []
        for frame_id, frame in self._frames().items():
            frame_nodes, frame_elements = self._nodes_in(frame_id, frame, observation_id)
            seen.extend(frame_nodes)
            elements.extend(frame_elements)

        # Filtered and renumbered together with the elements they name. Doing
        # the two separately is how a ref comes to point at the wrong control.
        kept: list[Node] = []
        self._handles = {}
        for position, original in enumerate(surviving_indices(seen)):
            node = seen[original]
            ref = NodeRef(observation_id=observation_id, value=f"{node.frame_id}:{position}")
            kept.append(_with_ref(node, ref))
            if elements[original] is not None:
                self._handles[ref.value] = int(elements[original] or 0)

        self._stamp(kept)

        self._current = Observation(
            observation_id=observation_id,
            nodes=tuple(kept),
            url_pattern=route_pattern(self.page.url),
            page_title=self.page.title(),
            captured_at=datetime.now(UTC),
        )
        return self._current

    def act(
        self,
        action_type: ActionType,
        node_ref: NodeRef | None,
        value: str | None = None,
    ) -> None:
        """Perform an action. Checked before the browser is touched."""
        self._require_open()
        if action_type is ActionType.READ:
            raise SurfaceError("read is not an action; call read()")

        if action_type is ActionType.NAVIGATE:
            if node_ref is not None:
                raise SurfaceError("navigate addresses a route, not a control")
            if value is None:
                raise SurfaceError("navigate needs a route to go to")
            self._run(lambda: self.page.goto(self._absolute(value), timeout=self._timeout))
            return

        if node_ref is None:
            raise SurfaceError(f"{action_type.value} needs a control to act on")

        locator = self._locator(node_ref)
        if action_type is ActionType.CLICK:
            self._run(lambda: locator.click(timeout=self._timeout))
        elif action_type is ActionType.TYPE:
            self._run(lambda: locator.fill(value or "", timeout=self._timeout))
        elif action_type is ActionType.SELECT:
            self._run(lambda: locator.select_option(value or "", timeout=self._timeout))

    def read(self, node_ref: NodeRef) -> str:
        """The value of one control, as a person would read it off the screen.

        Answered from the observation that named the ref, not by looking again.
        Observing mints a new observation, so a fresh look would make the ref
        it was handed stale the moment it arrived. The engine settles before
        every read, so the screen in hand is the current one.
        """
        self._require_open()
        return readable_value(self._require_current(node_ref))

    def screenshot(self) -> bytes:
        self._require_open()
        return self._run(lambda: bytes(self.page.screenshot(full_page=False)))

    def supported_signal_kinds(self) -> frozenset[SignalKind]:
        return PORTABLE_SIGNAL_KINDS

    def close(self) -> None:
        """Release the surface. Safe to call twice.

        The page and its context belong to whoever opened them, for the length
        of a run. Closing here only stops this object driving them.
        """
        self._closed = True

    # ---- perception --------------------------------------------------------

    def _frames(self) -> dict[str, Frame]:
        """Every frame, keyed by the identity a Node will carry.

        The main frame is called "main" so that a run against a page with no
        frames reads the same as one captured from it. Nested frames are keyed
        by url, which is what this application would need if it grew a frameset;
        it has none, so that path is written and not exercised.
        """
        frames = {MAIN_FRAME: self.page.main_frame}
        for frame in self.page.frames:
            if frame is not self.page.main_frame:
                frames[route_pattern(frame.url)] = frame
        return frames

    def _nodes_in(
        self, frame_id: str, frame: Frame, observation_id: str
    ) -> tuple[list[Node], list[int | None]]:
        session = self.page.context.new_cdp_session(frame.page)
        try:
            session.send("DOM.enable")
            session.send("DOM.getDocument")
            tree = session.send("Accessibility.getFullAXTree")
            destinations = _link_destinations(frame)

            nodes: list[Node] = []
            elements: list[int | None] = []
            for entry in tree.get("nodes", []):
                if not is_worth_keeping(entry):
                    continue
                backend_id = entry.get("backendDOMNodeId")
                placeholder = NodeRef(observation_id=observation_id, value=f"{frame_id}:?")
                node = node_from_ax(
                    entry,
                    ref=placeholder,
                    frame_id=frame_id,
                    bounds=_box_of(session, backend_id),
                    destination=None,
                )
                if node.role == "link" and node.name:
                    node = _with_destination(node, destinations.get(node.name))
                nodes.append(node)
                elements.append(int(backend_id) if backend_id else None)
            return nodes, elements
        finally:
            session.detach()

    def _stamp(self, nodes: list[Node]) -> None:
        """Mark each kept element with the ref that names it."""
        session = self.page.context.new_cdp_session(self.page)
        try:
            for node in nodes:
                backend_id = self._handles.get(node.ref.value)
                if backend_id is not None:
                    _set_attribute(session, backend_id, node.ref.value)
        finally:
            session.detach()

    # ---- action ------------------------------------------------------------

    def _locator(self, node_ref: NodeRef) -> Any:
        self._require_current(node_ref)
        return self.page.locator(f'[{REF_ATTRIBUTE}="{node_ref.value}"]')

    def _require_current(self, node_ref: NodeRef) -> Node:
        """The node this ref names on the screen in hand, or a refusal.

        A ref belongs to one observation. The stamp that makes it findable is
        rewritten on the next one, so a ref from an earlier screen names an
        element that has been replaced even when the page looks unchanged.
        """
        if self._current is None:
            raise SurfaceError("nothing has been observed yet")
        if node_ref.observation_id != self._current.observation_id:
            raise SurfaceError(
                f"node {node_ref.value!r} belongs to observation "
                f"{node_ref.observation_id!r}, but the screen is now "
                f"{self._current.observation_id!r}; re-observe before acting"
            )
        node = self._current.node(node_ref)
        if node is None:
            raise SurfaceError(f"no node {node_ref.value!r} on the current screen")
        return node

    def _run(self, action: Callable[[], T]) -> T:
        """Translate a driver failure into a domain one, and pass the value on.

        A Playwright error stops here. Letting it out would put a driver's
        exception type into the hands of every caller of the port, which is the
        coupling this layer exists to prevent.

        Everything that touches the page goes through this, not only act. A
        page that navigates away mid-observation or a browser that dies during
        a screenshot raise the same driver errors, and those reached callers
        untranslated while this only wrapped the acting half.
        """
        try:
            return action()
        except PlaywrightError as error:
            raise SurfaceError(str(error).splitlines()[0]) from error

    @property
    def _timeout(self) -> int:
        return self.action_timeout_ms

    def _absolute(self, route: str) -> str:
        if route.startswith(("http://", "https://")):
            return route
        return f"{self.base_url.rstrip('/')}{route}"

    def _require_open(self) -> None:
        if self._closed:
            raise SurfaceError("the surface is closed")


@contextmanager
def browser_session(
    base_url: str,
    headless: bool = True,
    viewport: Mapping[str, int] | None = None,
    action_timeout_ms: int = 5000,
    slow_mo_ms: int = 0,
) -> Iterator[PlaywrightSurface]:
    """A browser for the length of one run.

    Owned here rather than inside a step, because an escalation hands the same
    live session to a person and takes it back afterwards. A context created per
    step would have thrown away the thing they signed into.
    """
    size = dict(viewport or {"width": 1280, "height": 800})
    with sync_playwright() as playwright:
        # slow_mo only spaces out the driver's own calls. It changes what a
        # person can follow, not what the run does, so a watched run and an
        # unwatched one take the same path.
        browser = playwright.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        context = browser.new_context(viewport={"width": size["width"], "height": size["height"]})
        page = context.new_page()
        try:
            yield PlaywrightSurface(
                page=page, base_url=base_url, action_timeout_ms=action_timeout_ms
            )
        finally:
            context.close()
            browser.close()


def _link_destinations(frame: Frame) -> dict[str, str]:
    """Accessible name to destination, for anchors.

    Node.destination exists so policy can refuse a link before it is followed,
    which is only possible while the destination travels with the control. A
    name pointing at two different destinations is reported as unknown rather
    than guessed at.

    destination_pattern and not route_pattern: policy checks the origin of a
    destination against its allowlist, and a cross-origin link whose origin was
    stripped here arrives looking like a local route and is allowed.
    """
    seen: dict[str, str] = {}
    repeated: set[str] = set()
    for anchor in frame.query_selector_all("a[href]"):
        name = (anchor.inner_text() or "").strip()
        href = anchor.get_attribute("href") or ""
        if not name:
            continue
        destination = destination_pattern(href)
        if name in seen and seen[name] != destination:
            repeated.add(name)
        seen[name] = destination
    for name in repeated:
        seen.pop(name, None)
    return seen


def _box_of(session: Any, backend_node_id: int | None) -> Rect:
    if not backend_node_id:
        return ZERO_BOUNDS
    try:
        model = session.send("DOM.getBoxModel", {"backendNodeId": backend_node_id})["model"]
        border = model["border"]
        return Rect(
            x=float(border[0]),
            y=float(border[1]),
            width=float(model["width"]),
            height=float(model["height"]),
        )
    except PlaywrightError:
        # No box means nothing rendered, which the Node records as not visible.
        return ZERO_BOUNDS


def _set_attribute(session: Any, backend_node_id: int, value: str) -> None:
    try:
        resolved = session.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
        session.send(
            "Runtime.callFunctionOn",
            {
                "objectId": resolved["object"]["objectId"],
                "functionDeclaration": STAMP,
                "arguments": [{"value": value}, {"value": REF_ATTRIBUTE}],
            },
        )
    except PlaywrightError:
        # A node with no element behind it cannot be acted on anyway, and the
        # missing handle is what refuses the action later.
        return


def _with_destination(node: Node, destination: str | None) -> Node:
    return replace(node, destination=destination)


def _with_ref(node: Node, ref: NodeRef) -> Node:
    return replace(node, ref=ref)
