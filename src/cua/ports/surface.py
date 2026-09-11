"""The seam between perceiving a screen and the recorded flow that drives it.

The load-bearing boundary of the system. Everything above it is written against
Observation and NodeRef; everything below it knows about a browser, a desktop
accessibility tree, or a JSON fixture. The same capability artifact runs against
PlaywrightSurface and ScriptedSurface without modification, and that is the
proof the seam is real.

Two rules the implementations must honour, stated here because callers depend on
them and cannot check them:

  1. Native handles never cross this boundary. A NodeRef is opaque; the mapping
     from ref to a real element is private to the implementation.
  2. NodeRefs are scoped to the Observation that produced them. A caller that
     acts on a stale ref is a caller with a bug, and implementations should
     raise rather than resolve it against a newer page.
"""

from typing import Protocol

from cua.domain.actions import ActionType
from cua.domain.capability import SignalKind
from cua.domain.observation import NodeRef, Observation


class Surface(Protocol):
    """Perceive and act on one screen."""

    def observe(self) -> Observation:
        """Capture the current state as domain data.

        Raises SurfaceError if the screen cannot be read.
        """
        ...

    def act(
        self,
        action_type: ActionType,
        node_ref: NodeRef | None,
        value: str | None = None,
    ) -> None:
        """Perform a state-changing action.

        A command, not a query: it returns nothing, and failure is raised as
        SurfaceError rather than reported in a return value. The caller
        re-observes afterwards, which is what keeps perception in one place.

        `node_ref` is None only for NAVIGATE, where `value` carries the route.
        ActionType.READ is not accepted here; use `read`.
        """
        ...

    def read(self, node_ref: NodeRef) -> str:
        """The value of one control, as a person would read it off the screen.

        Reading is not an action. It takes no policy check, is not recorded as
        something the run did, and does not advance the application: observing
        does not change the world.

        There is no attribute parameter. Which field of a node holds the value
        is an accessibility tree detail, decided by
        domain.observation.readable_value so that every surface answers the same
        way, and a capability never has to mention it.
        """
        ...

    def screenshot(self) -> bytes:
        """Capture the current screen as image bytes.

        Present on Surface because only the surface can produce one. What is
        kept, and whether it is redacted, is EvidenceSink's decision.
        """
        ...

    def supported_signal_kinds(self) -> frozenset[SignalKind]:
        """Which target signal kinds this surface can evaluate.

        Lets resolution skip a surface-specific signal such as web.css rather
        than fail on it, and lets the skip be recorded as drift telemetry.

        Passed straight to resolve(), so it is the enum rather than strings: a
        surface that returned a name resolution does not recognise would silently
        skip a signal it was meant to honour.
        """
        ...

    def close(self) -> None:
        """Release the session. Owned at run scope, not step scope, so a human
        can take over the same live session. Must be safe to call twice.
        """
        ...
