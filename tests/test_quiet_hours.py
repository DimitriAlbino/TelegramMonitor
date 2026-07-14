"""Tests for quiet-hours evaluation (ADR-0005) — the pure decision function.

Covers same-day windows, cross-midnight windows, critical bypass, and the
unconfigured case. No DB, no clock — all times are injected.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tgmonitor.telegram.quiet_hours import is_in_quiet_window, should_defer


def dt(hour_utc: int, minute: int = 0) -> datetime:
    """A fixed UTC datetime at the given hour (the tz conversion happens inside)."""
    return datetime(2026, 7, 14, hour_utc, minute, tzinfo=UTC)


def test_unconfigured_returns_false() -> None:
    assert is_in_quiet_window(dt(3), None, "07:00", "UTC") is False
    assert is_in_quiet_window(dt(3), "22:00", None, "UTC") is False
    assert is_in_quiet_window(dt(3), "22:00", "07:00", None) is False


def test_same_day_window_inside() -> None:
    # 09:00-17:00 UTC; 12:00 is inside.
    assert is_in_quiet_window(dt(12), "09:00", "17:00", "UTC") is True


def test_same_day_window_outside() -> None:
    assert is_in_quiet_window(dt(8), "09:00", "17:00", "UTC") is False
    assert is_in_quiet_window(dt(17), "09:00", "17:00", "UTC") is False  # end is exclusive
    assert is_in_quiet_window(dt(20), "09:00", "17:00", "UTC") is False


def test_cross_midnight_window() -> None:
    # 22:00-07:00 UTC.
    assert is_in_quiet_window(dt(23), "22:00", "07:00", "UTC") is True
    assert is_in_quiet_window(dt(3), "22:00", "07:00", "UTC") is True
    assert is_in_quiet_window(dt(7), "22:00", "07:00", "UTC") is False  # end exclusive
    assert is_in_quiet_window(dt(12), "22:00", "07:00", "UTC") is False


def test_zero_length_window_is_false() -> None:
    assert is_in_quiet_window(dt(12), "09:00", "09:00", "UTC") is False


def test_timezone_applied() -> None:
    # 22:00-07:00 in America/New_York (UTC-4 in July). 02:00 UTC = 22:00 EDT → inside.
    assert is_in_quiet_window(dt(2), "22:00", "07:00", "America/New_York") is True
    # 12:00 UTC = 08:00 EDT → outside (past 07:00 end).
    assert is_in_quiet_window(dt(12), "22:00", "07:00", "America/New_York") is False


def test_invalid_tz_returns_false() -> None:
    assert is_in_quiet_window(dt(3), "22:00", "07:00", "Mars/Olympus") is False


def test_invalid_hhmm_returns_false() -> None:
    assert is_in_quiet_window(dt(3), "25:00", "07:00", "UTC") is False
    assert is_in_quiet_window(dt(3), "abc", "07:00", "UTC") is False


def test_critical_bypasses_quiet_hours() -> None:
    # Inside the window, but critical → not deferred.
    assert (
        should_defer(
            now_utc=dt(3), start_hhmm="22:00", end_hhmm="07:00", tz_name="UTC", critical=True
        )
        is False
    )


def test_non_critical_defers_inside_window() -> None:
    assert (
        should_defer(
            now_utc=dt(3), start_hhmm="22:00", end_hhmm="07:00", tz_name="UTC", critical=False
        )
        is True
    )


def test_non_critical_not_deferred_outside_window() -> None:
    assert (
        should_defer(
            now_utc=dt(12), start_hhmm="22:00", end_hhmm="07:00", tz_name="UTC", critical=False
        )
        is False
    )
