"""A surface driven by captured screens instead of a browser.

Reading JSON off disk is this adapter's mechanism, the same way walking an
accessibility tree is a browser adapter's. Both hand back an Observation, and
the domain type knows about neither.

The screens are real: every fixture under tests/fixtures/observations was
dumped from the running application rather than written by hand. A replay that
passes here is replaying against what the browser actually reports.

State advances on act, never on observe. Observing does not change the world,
so a script is the sequence of screens the application goes through, not the
sequence of calls the engine happens to make. The engine observes a variable
number of times per step — once to classify, again after acting to check the
checkpoint, again on every retry — and none of that should appear in a script.
Three things follow without being written: a retry re-observes the same screen,
as a genuinely slow page would; two consecutive reads both run against the page
they were meant for; and a script stays readable as a description of the
application rather than of the engine.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cua.domain.actions import ActionType
from cua.domain.capability import SignalKind
from cua.domain.errors import SurfaceError
from cua.domain.observation import Node, NodeRef, Observation, Rect, readable_value

# web.css needs a selector engine, which this surface does not have and a
# desktop surface never will. Declaring the portable kinds is what lets
# resolution skip it and record the skip rather than failing on it.
PORTABLE_SIGNAL_KINDS = frozenset(SignalKind) - {SignalKind.WEB_CSS}


def load_observation(path: Path) -> Observation:
    """Read one captured screen into domain types."""
    document = json.loads(path.read_text(encoding="utf-8"))
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


@dataclass(frozen=True)
class ActCall:
    """One action the surface was asked to perform.

    Recorded so a test can assert what a run did, and just as usefully what it
    did not: a policy refusal is only proven by an empty record.
    """

    action_type: ActionType
    node_ref: NodeRef | None
    value: str | None = None


class ScriptedSurface:
    """Plays a fixed sequence of screens and records what was done to them."""

    def __init__(
        self,
        screens: Sequence[Observation],
        supported_signal_kinds: frozenset[SignalKind] = PORTABLE_SIGNAL_KINDS,
    ) -> None:
        if not screens:
            raise ValueError("a scripted surface needs at least one screen")
        self._screens = tuple(screens)
        self._index = 0
        self._supported = supported_signal_kinds
        self._closed = False
        self.acted: list[ActCall] = []

    def open(self, url: str) -> Observation:
        """Attach to the first screen. The url is recorded and otherwise unused."""
        self.acted.append(ActCall(ActionType.NAVIGATE, None, url))
        return self._screens[self._index]

    def observe(self) -> Observation:
        return self._screens[self._index]

    def act(
        self,
        action_type: ActionType,
        node_ref: NodeRef | None,
        value: str | None = None,
    ) -> None:
        """Perform an action. Checked fully before anything is recorded.

        An action that could not have happened must not appear in `acted` and
        must not move the script on, because a test reads that record as what
        the run did.
        """
        if action_type is ActionType.READ:
            raise SurfaceError("read is not an action; call read()")

        if action_type is ActionType.NAVIGATE:
            if node_ref is not None:
                raise SurfaceError("navigate addresses a route, not a control")
        else:
            if node_ref is None:
                raise SurfaceError(f"{action_type.value} needs a control to act on")
            self._require_present(node_ref)

        self.acted.append(ActCall(action_type, node_ref, value))
        self._advance()

    def read(self, node_ref: NodeRef) -> str:
        """Read a control. Not recorded in `acted`, and does not advance."""
        return readable_value(self._require_present(node_ref))

    def screenshot(self) -> bytes:
        """A stand-in image. Enough for evidence to have something to attach."""
        return f"scripted:{self.observe().observation_id}".encode()

    def supported_signal_kinds(self) -> frozenset[SignalKind]:
        return self._supported

    def close(self) -> None:
        self._closed = True

    def _advance(self) -> None:
        """Move to the next screen, stopping at the last one.

        Clamped rather than raising. A test that acts once more than its script
        describes should fail on the assertion it was making, not with an
        IndexError from the fake.
        """
        self._index = min(self._index + 1, len(self._screens) - 1)

    def _require_present(self, node_ref: NodeRef) -> Node:
        """The node this ref names, or a refusal saying why it is not there.

        Two ways a ref goes bad, and a browser raises on both. The screen it
        came from may have been replaced, which makes the handle dead however
        well formed it looks. Or the screen is right and the ref names nothing
        on it. Acting on either would be acting on a control that is not there,
        so the fake refuses exactly where the real surface would.
        """
        current = self.observe()
        if node_ref.observation_id != current.observation_id:
            raise SurfaceError(
                f"node {node_ref.value!r} belongs to observation "
                f"{node_ref.observation_id!r}, but the screen is now "
                f"{current.observation_id!r}; re-observe before acting"
            )

        node = current.node(node_ref)
        if node is None:
            raise SurfaceError(f"no node {node_ref.value!r} on the current screen")
        return node
