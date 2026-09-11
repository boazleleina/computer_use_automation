"""Clock adapters make retry timing deterministic without making tests wait."""

from datetime import UTC, datetime, timedelta

from cua.adapters.clocks import FakeClock, RealClock


def test_fake_clock_starts_at_a_stable_utc_instant():
    clock = FakeClock()

    assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)
    assert clock.monotonic_ms() == 0
    assert clock.total_slept_ms == 0


def test_advance_moves_wall_and_monotonic_time_without_recording_a_sleep():
    start = datetime(2030, 6, 1, 12, 30, tzinfo=UTC)
    clock = FakeClock(start)

    clock.advance(1_250)

    assert clock.now() == start + timedelta(milliseconds=1_250)
    assert clock.monotonic_ms() == 1_250
    assert clock.slept == []


def test_sleep_records_each_wait_and_accumulates_elapsed_time():
    clock = FakeClock()

    clock.sleep(250)
    clock.sleep(750)

    assert clock.slept == [250, 750]
    assert clock.total_slept_ms == 1_000
    assert clock.monotonic_ms() == 1_000


def test_real_clock_reports_timezone_aware_time_and_monotonic_milliseconds():
    clock = RealClock()

    before = clock.monotonic_ms()
    now = clock.now()
    after = clock.monotonic_ms()

    assert now.tzinfo is UTC
    assert before <= after
