"""Time, real and fake.

The fake is the reason the port exists. A bounded retry tested against a real
clock takes as long as the bound it is testing, so either the suite is slow or
the bound is shortened for tests and the thing under test is no longer the
thing that ships.
"""

import time
from datetime import UTC, datetime, timedelta


class RealClock:
    """Wall-clock time and real waiting."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC wall-clock time."""
        return datetime.now(UTC)

    def monotonic_ms(self) -> int:
        """Return monotonic milliseconds from an arbitrary origin."""
        return int(time.monotonic() * 1000)

    def sleep(self, ms: int) -> None:
        """Block for the requested number of milliseconds."""
        time.sleep(ms / 1000)


class FakeClock:
    """Time that only moves when something asks it to.

    Sleeping returns immediately and advances the clock by exactly what was
    asked for, so a test can assert how long a run believed it waited without
    waiting. `slept` keeps the individual requests, because "retried twice" and
    "waited once for twice as long" are different behaviours.
    """

    def __init__(self, start: datetime | None = None) -> None:
        """Initialize simulated time, using a fixed UTC baseline by default."""
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._elapsed_ms = 0
        self.slept: list[int] = []

    def now(self) -> datetime:
        """Return the current simulated wall-clock time."""
        return self._now

    def monotonic_ms(self) -> int:
        """Return simulated milliseconds advanced since initialization."""
        return self._elapsed_ms

    def sleep(self, ms: int) -> None:
        """Record a delay and advance simulated time without blocking."""
        self.slept.append(ms)
        self.advance(ms)

    def advance(self, ms: int) -> None:
        """Move time on without anyone having waited for it."""
        self._elapsed_ms += ms
        self._now += timedelta(milliseconds=ms)

    @property
    def total_slept_ms(self) -> int:
        """Return the sum of delays requested through `sleep`."""
        return sum(self.slept)
