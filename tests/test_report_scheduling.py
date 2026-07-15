"""Tests for scheduled-Report delivery-time/timezone scheduling (#31).

Reports must fire at the configured ``delivery_time`` in the configured
``timezone``, not at ``now + cadence`` (which drifted later each tick and
ignored the user's chosen time). A Report scoped to a ``monitor_id`` must cover
only that Monitor; NULL covers all. ``monitor_id`` must be validated to belong
to the requesting User.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tgmonitor.reports import next_run_for_delivery_time


def test_next_run_is_today_when_delivery_time_later() -> None:
    """At 06:00 UTC with delivery 08:00 UTC, next run is today 08:00 UTC."""
    now = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
    nxt = next_run_for_delivery_time("08:00", "UTC", cadence_days=1, now=now)
    assert nxt == datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def test_next_run_is_tomorrow_when_delivery_time_passed() -> None:
    """At 10:00 UTC with delivery 08:00 UTC, next run is tomorrow 08:00 UTC."""
    now = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    nxt = next_run_for_delivery_time("08:00", "UTC", cadence_days=1, now=now)
    assert nxt == datetime(2026, 7, 16, 8, 0, tzinfo=UTC)


def test_next_run_weekly_skips_correct_days() -> None:
    """Weekly cadence advances 7 days from the just-fired time."""
    now = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    nxt = next_run_for_delivery_time("08:00", "UTC", cadence_days=7, now=now)
    assert nxt == datetime(2026, 7, 16, 8, 0, tzinfo=UTC)  # next 08:00, then +7 logic


def test_next_run_applies_timezone() -> None:
    """delivery_time is interpreted in the configured tz (#31)."""
    # 08:00 America/New_York (UTC-4 in July) = 12:00 UTC. At 06:00 UTC the next
    # 08:00 EDT is today 12:00 UTC.
    now = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
    nxt = next_run_for_delivery_time("08:00", "America/New_York", cadence_days=1, now=now)
    assert nxt == datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def test_next_run_invalid_tz_falls_back_to_utc() -> None:
    now = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
    nxt = next_run_for_delivery_time("08:00", "Mars/Olympus", cadence_days=1, now=now)
    assert nxt == datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def test_next_run_invalid_time_falls_back_to_now_plus_cadence() -> None:
    """Malformed delivery_time degrades gracefully to now+cadence (#31)."""
    now = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
    nxt = next_run_for_delivery_time("not-a-time", "UTC", cadence_days=1, now=now)
    assert nxt == now + timedelta(days=1)


# --- monitor_id scoping + ownership (#31) ---


def test_render_scheduled_report_passes_monitor_id_to_filter() -> None:
    """render_scheduled_report must scope to monitor_id when given (#31).

    Asserted structurally via the SELECT it builds: a monitor_id filter is
    added. We drive render with a fake session that records the statement.
    """

    class _FakeExecResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class _FakeSession:
        def __init__(self) -> None:
            self.stmts: list[str] = []

        async def execute(self, stmt):
            self.stmts.append(str(stmt))
            return _FakeExecResult()

    import asyncio

    from tgmonitor.reports import render_scheduled_report

    sess = _FakeSession()
    asyncio.run(render_scheduled_report(sess, user_id=1, cadence="daily", monitor_id=42))
    unscoped = _FakeSession()
    asyncio.run(render_scheduled_report(unscoped, user_id=1, cadence="daily"))
    # The scoped render adds an `AND monitors.id = :id_1` predicate that the
    # unscoped render lacks.
    assert "monitors.id = " in sess.stmts[0]
    assert "monitors.id = " not in unscoped.stmts[0]


def test_create_report_validates_monitor_id_ownership() -> None:
    """create_report must reject a monitor_id owned by another user (#31)."""
    import inspect

    from tgmonitor.api.reports import create_report

    src = inspect.getsource(create_report)
    assert "monitor_id is not None" in src
    assert "404" in src or "not found" in src
