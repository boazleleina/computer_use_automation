"""Time as a dependency.

The smallest port, and the one most often skipped. Without it, testing a
bounded retry means waiting for the bound, and every recorded run carries a
timestamp that differs from the last, so no two evidence files diff cleanly.

With it, a fake clock advances instantly and the domain suite finishes in under
a second with real timeout semantics rather than shortened ones.
"""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Wall-clock time, elapsed time, and waiting."""

    def now(self) -> datetime:
        """Current time, timezone-aware. Used for timestamps in evidence."""
        ...

    def monotonic_ms(self) -> int:
        """Milliseconds since an arbitrary origin.

        Separate from `now` because wall-clock time can move backwards when the
        host synchronises, which would make a measured duration negative. Use
        this for deadlines and elapsed time, never `now`.
        """
        ...

    def sleep(self, ms: int) -> None:
        """Wait. Implementations under test return immediately."""
        ...
