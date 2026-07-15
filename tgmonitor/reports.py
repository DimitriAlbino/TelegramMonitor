"""Scheduled Report rendering + the worker's report scheduler (ADR-0006).

- :func:`render_scheduled_report` reads the hourly/daily rollups and formats a
  per-Monitor digest (uptime %, Incident count, worst latency, current state).
- :func:`render_post_incident_summary` formats the structured follow-up sent
  after an Incident closes.
- :func:`run_report_tick` is the scheduler: claim due reports, render, deliver,
  advance next_run_at (same DB-driven pattern as Checks).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from html import escape

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.incident_model import Incident
from tgmonitor.models import Check, Monitor
from tgmonitor.report_model import Report
from tgmonitor.telegram.client import NotificationChannel

log = logging.getLogger("tgmonitor.reports")

# How far back each cadence looks.
PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


def _esc(text: str) -> str:
    """HTML-escape user-controlled text interpolated into parse_mode=HTML (#28)."""
    return escape(text, quote=True)


def _advance_next_run(cadence: str, now: datetime) -> datetime:
    delta = timedelta(days=PERIOD_DAYS.get(cadence, 1))
    return now + delta


async def render_scheduled_report(session: AsyncSession, user_id: int, cadence: str) -> str:
    """Render a periodical digest for the user's monitors over the cadence window."""
    days = PERIOD_DAYS.get(cadence, 1)
    since = datetime.now(UTC) - timedelta(days=days)

    monitors = (
        (
            await session.execute(
                select(Monitor).where(Monitor.user_id == user_id).order_by(Monitor.id)
            )
        )
        .scalars()
        .all()
    )

    if not monitors:
        return f"<b>{cadence.title()} report</b>\nYou have no monitors."

    lines = [f"<b>{cadence.title()} report</b> (last {days}d)"]
    for m in monitors:
        # Aggregate from raw checks over the window (the continuous aggregates
        # back this, but a direct query is simpler and correct for the launch
        # envelope; the rollups keep long-term history cheap).
        stats = (
            await session.execute(
                select(
                    func.count().label("total"),
                    func.count().filter(Check.success).label("ok"),
                    func.max(Check.latency_ms).label("worst"),
                ).where(Check.monitor_id == m.id, Check.checked_at >= since)
            )
        ).one()
        total, ok, worst = stats.total, stats.ok, stats.worst
        uptime = (ok * 100 / total) if total and total > 0 else 100.0
        # Incident count over the window.
        inc_count = (
            await session.execute(
                select(func.count())
                .select_from(Incident)
                .where(Incident.monitor_id == m.id, Incident.opened_at >= since)
            )
        ).scalar_one()
        # Current state: latest check.
        latest = (
            (
                await session.execute(
                    select(Check)
                    .where(Check.monitor_id == m.id)
                    .order_by(Check.checked_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        state = "🟢 up" if (latest and latest.success) else "🔴 down"
        worst_str = f"{worst}ms" if worst is not None else "-"
        lines.append(
            f"• <b>{_esc(m.name)}</b> — {state} | uptime {uptime:.1f}% | "
            f"{inc_count} incident(s) | worst {worst_str}"
        )
    return "\n".join(lines)


async def render_post_incident_summary(session: AsyncSession, incident: Incident) -> str:
    """Render the structured follow-up sent after an Incident closes."""
    monitor = await session.get(Monitor, incident.monitor_id)
    name = _esc(monitor.name) if monitor else f"#{incident.monitor_id}"
    # Count failed checks during the incident span.
    end = incident.closed_at or datetime.now(UTC)
    failed = (
        await session.execute(
            select(func.count())
            .select_from(Check)
            .where(
                Check.monitor_id == incident.monitor_id,
                Check.checked_at >= incident.opened_at,
                Check.checked_at <= end,
                ~Check.success,
            )
        )
    ).scalar_one()
    duration = end - incident.opened_at
    minutes = int(duration.total_seconds() // 60)
    return (
        f"📋 <b>Post-incident summary</b>\n"
        f"Monitor: <b>{name}</b>\n"
        f"Duration: {minutes} min\n"
        f"Reason: {_esc(incident.open_reason)}\n"
        f"Failed checks: {failed}"
    )


CLAIM_DUE_REPORTS_SQL = text(
    """
    UPDATE reports
       SET next_run_at = now() + (
           CASE cadence
             WHEN 'daily'   THEN INTERVAL '1 day'
             WHEN 'weekly'  THEN INTERVAL '7 days'
             WHEN 'monthly' THEN INTERVAL '30 days'
             ELSE           INTERVAL '1 day'
           END
       )
     WHERE next_run_at <= now()
       AND id IN (
           SELECT id FROM reports WHERE next_run_at <= now()
           ORDER BY next_run_at LIMIT :limit FOR UPDATE SKIP LOCKED
       )
    RETURNING id, user_id, cadence
    """
)


async def run_report_tick(
    session_factory_fn: object,
    channel: NotificationChannel | None = None,
    batch_limit: int = 50,
) -> int:
    """Claim due reports, render, deliver, and advance next_run_at.

    Returns the number of reports dispatched. Called from the worker's tick loop
    (or its own cadence). Never raises — a failure is logged.
    """
    from tgmonitor.db import session_factory as sf

    ch = channel or NotificationChannel()
    from tgmonitor.models import User

    async with sf()() as session:
        rows = (await session.execute(CLAIM_DUE_REPORTS_SQL, {"limit": batch_limit})).all()
        await session.commit()

    for r in rows:
        try:
            async with sf()() as session:
                report = await session.get(Report, r.id)
                if report is None:
                    continue
                body = await render_scheduled_report(session, r.user_id, r.cadence)
                report.last_body = body
                report.last_rendered_at = datetime.now(UTC)
                report.last_run_at = datetime.now(UTC)
                # Resolve the user's chat and deliver.
                user = await session.get(User, r.user_id)
                await session.commit()
                if user and user.telegram_chat_id:
                    await ch.send(user.telegram_chat_id, body)
                else:
                    log.info(
                        "report %s: user %s has no linked chat; rendered only", r.id, r.user_id
                    )
        except Exception:
            log.exception("failed to dispatch report %s", r.id)
    return len(rows)
