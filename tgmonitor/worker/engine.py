"""The tick-loop Check engine (ADR-0004).

Design invariants:

1. **Scheduling state lives in the DB.** The only in-memory state is the
   running task set and the global semaphore. On restart, the worker simply
   resumes reading ``next_check_at`` — no rehydration, no double-fire window.

2. **Claim-then-run is atomic.** A single ``UPDATE ... WHERE next_check_at <=
   now() ... RETURNING`` advances the cursor *and* selects the monitor in one
   statement. Two ticks racing on the same monitor: the first wins the row, the
   second sees the already-advanced cursor and skips it. This is what lets the
   loop be simple (no job table, no locks held across the network probe).

3. **The engine never dies on one bad monitor.** Each Check runs in its own
   task; exceptions are caught and logged, yielding a failing Result row where
   possible. A task that errors before writing still leaves the cursor advanced
   (from the claim), so it is not re-run immediately.

4. **Concurrency is bounded.** A global ``asyncio.Semaphore(CHECK_CONCURRENCY)``
   caps in-flight probes. Due monitors beyond the cap wait in the claim set and
   run on the next tick — the claim already advanced their cursor by one
   interval, which is the correct back-off.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.config import Settings, get_settings
from tgmonitor.db import dispose_engine, session_factory
from tgmonitor.executors.base import CheckConfig, Transport
from tgmonitor.executors.http import HttpTransport
from tgmonitor.models import Check
from tgmonitor.results import Result

log = logging.getLogger("tgmonitor.worker")


# A claimed Monitor: enough to run the Check without touching the ORM in the
# probe path. Built from the atomic claim statement's RETURNING columns.
@dataclass(frozen=True, slots=True)
class ClaimedMonitor:
    id: int
    name: str
    check_kind: str
    target: str
    expected_status: int
    body_contains: str | None
    max_latency_ms: int | None
    timeout_s: float

    def to_check_config(self) -> CheckConfig:
        return CheckConfig(
            monitor_id=self.id,
            check_kind=self.check_kind,
            target=self.target,
            expected_status=self.expected_status,
            body_contains=self.body_contains,
            max_latency_ms=self.max_latency_ms,
            timeout_s=self.timeout_s,
        )


# Claim a batch of due, unpaused monitors and advance their cursors atomically.
# ``now()`` is evaluated once per statement by Postgres, so all claimed monitors
# get the same baseline. LIMIT bounds one tick's work; a backlog is drained over
# successive ticks rather than one giant batch.
CLAIM_DUE_SQL = text(
    """
    UPDATE monitors
       SET next_check_at = now() + (interval_s || ' seconds')::interval
     WHERE paused = false
       AND next_check_at <= now()
       AND id IN (
           SELECT id FROM monitors
            WHERE paused = false AND next_check_at <= now()
            ORDER BY next_check_at
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
       )
    RETURNING id, name, check_kind, target,
              (config->>'expected_status')::int     AS expected_status,
              config->>'body_contains'              AS body_contains,
              NULLIF(config->>'max_latency_ms','')::int AS max_latency_ms,
              COALESCE(NULLIF(config->>'timeout_s','')::float, 10.0) AS timeout_s
    """
)


async def claim_due_monitors(session: AsyncSession, batch_limit: int) -> list[ClaimedMonitor]:
    """Atomically claim up to ``batch_limit`` due monitors and advance cursors.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers (the future horizontal
    scale-out) don't collide. The cursor advance is in the same statement, so a
    monitor is never double-claimed.
    """
    result = await session.execute(CLAIM_DUE_SQL, {"limit": batch_limit})
    rows = result.all()
    return [
        ClaimedMonitor(
            id=r.id,
            name=r.name,
            check_kind=r.check_kind,
            target=r.target,
            expected_status=r.expected_status if r.expected_status is not None else 200,
            body_contains=r.body_contains,
            max_latency_ms=r.max_latency_ms,
            timeout_s=r.timeout_s if r.timeout_s is not None else 10.0,
        )
        for r in rows
    ]


async def persist_result(session: AsyncSession, monitor_id: int, result: Result) -> None:
    """Append one Result row to the checks hypertable."""
    checked_at = (
        datetime.fromtimestamp(result.checked_at, tz=UTC)
        if result.checked_at is not None
        else datetime.now(UTC)
    )
    session.add(
        Check(
            monitor_id=monitor_id,
            checked_at=checked_at,
            success=result.success,
            status_code=result.status_code,
            latency_ms=result.latency_ms,
            reason=result.reason,
        )
    )
    await session.commit()


class CheckEngine:
    """The tick-loop engine. Owns the semaphore, the transport, and the loop.

    The transport is injected (default: a long-lived ``httpx`` client) so tests
    can run the loop against a fake without real I/O.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        transport: Transport | None = None,
        batch_limit: int = 500,
    ) -> None:
        self.settings = settings or get_settings()
        self.batch_limit = batch_limit
        self._semaphore = asyncio.Semaphore(self.settings.check_concurrency)
        self._transport: Transport = transport or HttpTransport(httpx.AsyncClient(timeout=30.0))
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    async def _run_one(self, monitor: ClaimedMonitor) -> None:
        """Run one Check under the semaphore and persist its Result.

        Never raises: any exception becomes a failing Result with a reason, so
        one bad monitor can't take the engine down.
        """
        async with self._semaphore:
            from tgmonitor.executors.base import run_check

            try:
                result = await run_check(monitor.to_check_config(), self._transport)
                result = _stamp_checked_at(result)
            except Exception as exc:
                log.exception("check for monitor %s raised", monitor.id)
                result = Result(
                    success=False,
                    reason=f"executor error: {exc.__class__.__name__}: {exc}",
                    checked_at=datetime.now(UTC).timestamp(),
                )
            try:
                async with session_factory()() as session:
                    await persist_result(session, monitor.id, result)
            except Exception:
                log.exception("failed to persist result for monitor %s", monitor.id)

    async def tick(self) -> int:
        """Run one tick: claim due monitors, spawn a task per claim.

        Returns the number of monitors claimed this tick (for tests/logging).

        The claim *must* commit before the session closes — the ``next_check_at``
        advance is what prevents the same monitor being re-claimed every tick.
        The probe itself runs after the commit, so a slow or crashed Check does
        not hold the scheduling cursor back.
        """
        async with session_factory()() as session:
            claimed = await claim_due_monitors(session, self.batch_limit)
            await session.commit()

        for monitor in claimed:
            task = asyncio.create_task(self._run_one(monitor), name=f"check-{monitor.id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if claimed:
            log.info("tick claimed %d monitors", len(claimed))
        return len(claimed)

    async def run(self) -> None:
        """Run the tick loop until :meth:`stop` is called (or SIGTERM)."""
        log.info(
            "check engine starting (tick=%.1fs concurrency=%d)",
            self.settings.check_tick_interval_s,
            self.settings.check_concurrency,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows lacks add_signal_handler
                loop.add_signal_handler(sig, self.stop)

        while not self._stopping:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(self.settings.check_tick_interval_s)

        # Graceful shutdown: let in-flight checks finish before exiting.
        if self._tasks:
            log.info("draining %d in-flight checks", len(self._tasks))
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await dispose_engine()
        log.info("check engine stopped")

    def stop(self) -> None:
        self._stopping = True


def _stamp_checked_at(result: Result) -> Result:
    """Ensure the Result carries a checked_at timestamp (for storage)."""
    if result.checked_at is not None:
        return result
    return Result(
        success=result.success,
        reason=result.reason,
        latency_ms=result.latency_ms,
        status_code=result.status_code,
        checked_at=datetime.now(UTC).timestamp(),
    )
