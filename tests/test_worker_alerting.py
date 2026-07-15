"""Tests for the worker alerting bridge (Seam between the pure SM and persistence).

Covers the concurrency guard (#32): per-monitor serialization so two concurrent
Checks for one Monitor cannot lose a failure increment through a
read-modify-write race. Uses a recording fake session; no real DB.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tgmonitor.worker.alerting import apply_transition


class _FakeResult:
    """Mimics the bits of a SQLAlchemy Result apply_transition uses."""

    def __init__(self, scalar: object | None = None, all_rows: list | None = None) -> None:
        self._scalar = scalar
        self._all = all_rows or []

    def scalar_one(self) -> object:
        return self._scalar  # type: ignore[return-value]

    def scalars(self):
        return self

    def first(self):
        return self._scalar

    def all(self):
        return self._all


class _FakeSession:
    """Records every execute() call (statement + params) in order.

    apply_transition issues, in order: the advisory lock, the state UPDATE,
    and the recent_opens SELECT. We assert the lock precedes everything.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        # Configurable responses keyed by a substring of the SQL text.
        self.scalar_responses: dict[str, object] = {}
        self.add = AsyncMock()
        self.flush = AsyncMock()

    async def refresh(self, obj, attribute_names=None):
        # Record the re-read so tests can assert it happens under the lock (#32).
        self.executed.append(("REFRESH", attribute_names))

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params))
        # Default: empty-ish result.
        for needle, val in self.scalar_responses.items():
            if needle in sql:
                return _FakeResult(scalar=val)
        return _FakeResult(scalar=None, all_rows=[])

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_apply_transition_takes_per_monitor_lock_first() -> None:
    """The first statement issued must be the per-monitor advisory lock (#32).

    Without serialization, two concurrent Checks for one Monitor read the same
    consecutive_failures and both write the same incremented value, losing a
    failure and delaying the Incident open by an interval.
    """
    session = _FakeSession()

    # A monitor-like object exposing the columns apply_transition reads.
    class M:
        id = 42
        failure_threshold = 3
        recovery_threshold = 2
        incident_status = "ok"
        consecutive_failures = 0
        consecutive_successes = 0
        muted = False

    await apply_transition(session, M(), True, "ok")
    assert session.executed, "apply_transition should issue SQL"
    first_sql = session.executed[0][0]
    assert "pg_advisory_xact_lock" in first_sql, (
        "concurrent transitions must serialize on a per-monitor advisory lock"
    )
    # The counter re-read (#32) must happen AFTER the lock and BEFORE the state
    # UPDATE — otherwise the lock serializes writes but both start from the same
    # stale pre-lock value and one failure increment is lost.
    steps = [s[0] for s in session.executed]
    lock_i = next(i for i, s in enumerate(steps) if "pg_advisory_xact_lock" in s)
    refresh_i = next(i for i, s in enumerate(steps) if s == "REFRESH")
    update_i = next(i for i, s in enumerate(steps) if "UPDATE" in s)
    assert lock_i < refresh_i < update_i, (
        "counter must be refreshed under the lock, before the write (#32)"
    )


# --- Post-incident summary stamping (#21) ---
#
# The summary must actually be sent before summary_sent_at is stamped; today the
# close path stamps the flag before the sink runs (and the sink gates on the
# flag, so the send is always skipped), then records the summary as sent with no
# retry. The flag is also set when the monitor is muted or delivery was deferred.


@pytest.mark.asyncio
async def test_sink_does_not_receive_summary_via_pre_stamp() -> None:
    """summary_sent_at must not be set before the sink confirms delivery (#21).

    The bridge must return the CLOSE intent for post-commit delivery and leave
    the (mocked) Incident's summary_sent_at untouched on close (the sink stamps
    it only after a confirmed send). apply_transition itself no longer sends
    anything (#41); it returns the intents the engine delivers after commit.
    """
    session = _FakeSession()
    # A fake open incident for _latest_open_incident to find and close.
    from datetime import UTC, datetime

    class _Inc:
        id = 5
        monitor_id = 7
        opened_at = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
        closed_at = None
        close_reason = None
        outcome = None
        summary_sent_at = None
        failed_check_count = None

    fake_incident = _Inc()
    session.scalar_responses["incidents"] = fake_incident
    # _count_failed_checks query returns 0.
    session.scalar_responses["count"] = 0

    class M2:
        id = 7
        failure_threshold = 1
        recovery_threshold = 1
        incident_status = "down"
        consecutive_failures = 0
        consecutive_successes = 0
        muted = False

    intents = await apply_transition(session, M2(), True, "recovered")
    close_intents = [i for i in intents if i.action.value == "close_incident"]
    assert close_intents, "a recovery should return a CLOSE_INCIDENT intent"
    assert close_intents[0].incident_id == 5
    # The bridge must NOT have pre-stamped summary_sent_at (#21).
    assert fake_incident.summary_sent_at is None


@pytest.mark.asyncio
async def test_deliver_alerts_suppressed_when_muted() -> None:
    """deliver_alerts must not call the sink for a muted monitor (#41)."""
    from tgmonitor.incidents import Action
    from tgmonitor.worker.alerting import AlertIntent, deliver_alerts

    calls: list = []

    async def sink(intent):
        calls.append(intent)

    intent = AlertIntent(monitor_id=7, action=Action.OPEN_INCIDENT, incident_id=1, reason="down")
    await deliver_alerts(sink, [intent], muted=True)
    assert calls == [], "muted monitor must not deliver"
    await deliver_alerts(sink, [intent], muted=False)
    assert calls == [intent], "un-muted monitor must deliver"
