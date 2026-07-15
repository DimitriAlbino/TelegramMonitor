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
from tgmonitor.worker.alerting import AlertSink

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
    follow_redirects: bool
    tcp_timeout_s: float
    json_field_path: str | None
    json_keyword: str | None

    def to_check_config(self) -> CheckConfig:
        return CheckConfig(
            monitor_id=self.id,
            check_kind=self.check_kind,
            target=self.target,
            expected_status=self.expected_status,
            body_contains=self.body_contains,
            max_latency_ms=self.max_latency_ms,
            timeout_s=self.timeout_s,
            follow_redirects=self.follow_redirects,
            tcp_timeout_s=self.tcp_timeout_s,
            json_field_path=self.json_field_path,
            json_keyword=self.json_keyword,
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
              COALESCE((config->>'expected_status')::int, 200) AS expected_status,
              config->>'body_contains'              AS body_contains,
              NULLIF(config->>'max_latency_ms','')::int AS max_latency_ms,
              COALESCE(NULLIF(config->>'timeout_s','')::float, 10.0) AS timeout_s,
              COALESCE((config->>'follow_redirects')::bool, false) AS follow_redirects,
              COALESCE(NULLIF(config->>'tcp_timeout_s','')::float,
                       NULLIF(config->>'timeout_s','')::float, 5.0) AS tcp_timeout_s,
              config->>'json_field_path' AS json_field_path,
              config->>'json_keyword'    AS json_keyword
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
            follow_redirects=r.follow_redirects if r.follow_redirects is not None else False,
            tcp_timeout_s=r.tcp_timeout_s if r.tcp_timeout_s is not None else 5.0,
            json_field_path=r.json_field_path,
            json_keyword=r.json_keyword,
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
    """The tick-loop engine. Owns the semaphore, the per-kind transports, and
    the loop.

    Transports are injected per kind (default: a long-lived ``httpx`` client for
    HTTP, ``asyncio`` sockets for TCP) so tests can run the loop against fakes.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        transport: Transport | None = None,
        transports: dict[str, Transport] | None = None,
        batch_limit: int = 500,
        alert_sink: AlertSink | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.batch_limit = batch_limit
        self._semaphore = asyncio.Semaphore(self.settings.check_concurrency)
        self._alert_sink: AlertSink | None = alert_sink
        # A transport per kind. The single ``transport`` arg (for back-compat and
        # simple tests) overrides the http entry; ``transports`` is the general form.
        from tgmonitor.executors.tcp import TcpTransport

        self._transports: dict[str, Transport] = {
            # follow_redirects=True at the client level is the reliable default;
            # per-request follow_redirects=False correctly overrides it for
            # monitors that don't want redirects. Setting it only per-request
            # is unreliable when connections are pooled and reused (httpx may
            # return a cached redirect response from the pool).
            "http": transport or HttpTransport(httpx.AsyncClient(timeout=30.0, follow_redirects=True)),
            "tcp": TcpTransport(),
        }
        if transports:
            self._transports.update(transports)
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def _transport_for(self, kind: str) -> Transport:
        return self._transports.get(kind, self._transports["http"])

    async def _run_one(self, monitor: ClaimedMonitor) -> None:
        """Run one Check under the semaphore, persist its Result, and run the
        Incident state machine.

        Never raises: any exception becomes a failing Result with a reason, so
        one bad monitor can't take the engine down. The state machine (ADR-0005)
        runs after the Result is persisted and may open/close an Incident and
        emit an Alert through the configured sink (set by T4's bot wiring).
        """
        async with self._semaphore:
            from tgmonitor.executors.base import run_check

            transport = self._transport_for(monitor.check_kind)
            try:
                result = await run_check(monitor.to_check_config(), transport)
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
                    # Run the Incident state machine: load the Monitor row, apply
                    # the transition, persist Incident open/close, emit alerts.
                    from tgmonitor.models import Monitor as MonitorModel
                    from tgmonitor.worker.alerting import apply_transition

                    mon = await session.get(MonitorModel, monitor.id)
                    if mon is not None and not mon.paused:
                        await apply_transition(
                            session,
                            mon,
                            result.success,
                            result.reason,
                            alert_sink=self._alert_sink,
                        )
                    await session.commit()
            except Exception:
                log.exception("failed to persist/transition for monitor %s", monitor.id)

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
            # Run the report scheduler each tick too (ADR-0006): it claims due
            # reports, renders the digest, delivers, advances next_run_at.
            try:
                from tgmonitor.reports import run_report_tick

                await run_report_tick(None)
            except Exception:
                log.exception("report tick failed")
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
