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

    await apply_transition(session, M(), True, "ok", alert_sink=None)
    assert session.executed, "apply_transition should issue SQL"
    first_sql = session.executed[0][0]
    assert "pg_advisory_xact_lock" in first_sql, (
        "concurrent transitions must serialize on a per-monitor advisory lock"
    )
