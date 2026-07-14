"""Quiet hours evaluation (ADR-0005).

A pure function :func:`is_in_quiet_window` decides whether a given moment falls
within a user's quiet window, accounting for windows that cross midnight (e.g.
22:00-07:00). The decision drives delivery: non-critical Alerts during the
window are deferred into a digest; critical Monitors bypass.

The digest itself is a simple list of queued AlertIntents flushed when the
window ends. For the launch envelope (single VPS, single worker) an in-process
list keyed by user id is sufficient; the window-end flush is driven by the
worker's tick loop checking each tick whether the window just ended.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


def _parse_hhmm(s: str | None) -> time | None:
    if not s or len(s) != 5 or s[2] != ":":
        return None
    try:
        hh, mm = int(s[:2]), int(s[3:])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return time(hh, mm)


def is_in_quiet_window(
    now_utc: datetime,
    start_hhmm: str | None,
    end_hhmm: str | None,
    tz_name: str | None,
) -> bool:
    """True if ``now_utc`` falls within the quiet window in the user's tz.

    Returns False if quiet hours are not configured (any of start/end/tz missing)
    or unparseable. Handles windows crossing midnight (start > end).
    """
    start = _parse_hhmm(start_hhmm)
    end = _parse_hhmm(end_hhmm)
    if start is None or end is None or not tz_name:
        return False
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, ValueError):
        return False
    local = now_utc.astimezone(tz)
    current = local.time()

    if start == end:
        return False  # zero-length window
    if start < end:
        # Same-day window, e.g. 09:00-17:00.
        return start <= current < end
    # Crosses midnight, e.g. 22:00-07:00.
    return current >= start or current < end


def should_defer(
    *,
    now_utc: datetime,
    start_hhmm: str | None,
    end_hhmm: str | None,
    tz_name: str | None,
    critical: bool,
) -> bool:
    """Decide whether an Alert should be deferred into the digest.

    Critical Monitors bypass quiet hours (ADR-0005); non-critical Alerts defer
    when inside the window. Recovery Alerts follow the same rule as their
    opening Alert, so the caller passes the monitor's critical flag for both.
    """
    if critical:
        return False
    return is_in_quiet_window(now_utc, start_hhmm, end_hhmm, tz_name)
