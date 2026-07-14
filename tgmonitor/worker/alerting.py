"""Bridge the pure Incident state machine to persistence + Alert delivery.

After each Check Result is persisted, the engine calls :func:`apply_transition`
here. This function:
1. Loads the Monitor's incident-tracking state from its row.
2. Calls the pure :func:`tgmonitor.incidents.step`.
3. Writes the new state back to the Monitor row.
4. Persists Incident open/close (the Incident table).
5. Emits an Alert action via the injected ``alert_sink`` callback — which T4's
   Telegram bot satisfies. Mute is honoured here (delivery layer), never in the
   pure state machine.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.incident_model import Incident
from tgmonitor.incidents import Action, CheckOutcome, MonitorState, Thresholds, step
from tgmonitor.models import Check, Monitor

log = logging.getLogger("tgmonitor.worker.alerting")

# A sink consumes an Alert intent. The real implementation (T4) sends over
# Telegram; tests inject a recording fake. Async because delivery is I/O.
AlertSink = Callable[["AlertIntent"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AlertIntent:
    """What the delivery layer should send for one Incident transition."""

    monitor_id: int
    action: Action
    incident_id: int | None
    reason: str


def _thresholds_for(monitor: Monitor) -> Thresholds:
    return Thresholds(
        failure_threshold=monitor.failure_threshold,
        recovery_threshold=monitor.recovery_threshold,
    )


def _state_from(monitor: Monitor) -> MonitorState:
    """Reconstruct the SM state from the persisted authoritative columns.

    ``incident_status`` is the source of truth (ok/down/flapping); the counters
    are persisted verbatim so a down monitor accumulating recovery successes
    retains them across ticks.
    """
    return MonitorState(
        status=monitor.incident_status,  # type: ignore[arg-type]
        consecutive_failures=monitor.consecutive_failures,
        consecutive_successes=monitor.consecutive_successes,
    )


async def _recent_opens(session: AsyncSession, monitor_id: int, window_s: float) -> list[float]:
    """Timestamps of Incident opens within the flapping window, newest last."""
    cutoff = datetime.now(UTC).timestamp() - window_s
    stmt = (
        select(Incident.opened_at)
        .where(Incident.monitor_id == monitor_id)
        .order_by(Incident.opened_at.desc())
        .limit(50)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [r.timestamp() for r in rows if r.timestamp() >= cutoff][::-1]


async def apply_transition(
    session: AsyncSession,
    monitor: Monitor,
    result_success: bool,
    result_reason: str,
    *,
    alert_sink: AlertSink | None,
) -> Action:
    """Run the state machine for one Monitor after a Check, persist, and alert.

    Returns the action taken (for logging). Never raises — a failure here is
    logged and swallowed so the engine keeps running.
    """
    th = _thresholds_for(monitor)
    state = _state_from(monitor)
    state.recent_opens = await _recent_opens(session, monitor.id, th.flap_window_s)
    now = datetime.now(UTC).timestamp()

    outcome = CheckOutcome(success=result_success, reason=result_reason)
    transition = step(state, outcome, th, now=now)

    # Persist the new counters + authoritative status back to the monitor row.
    # The counters are the SM's own values; the status column makes the next
    # tick's reconstruction exact (no heuristic).
    new_state = transition.state
    await session.execute(
        update(Monitor)
        .where(Monitor.id == monitor.id)
        .values(
            consecutive_failures=new_state.consecutive_failures,
            consecutive_successes=new_state.consecutive_successes,
            incident_status=new_state.status,
        )
    )

    action = transition.action

    # Persist Incident transitions + emit alerts.
    if action is Action.OPEN_INCIDENT:
        incident = Incident(monitor_id=monitor.id, open_reason=result_reason)
        session.add(incident)
        await session.flush()
        await _maybe_alert(
            alert_sink, AlertIntent(monitor.id, action, incident.id, result_reason), monitor
        )
    elif action is Action.CLOSE_INCIDENT:
        # Close the most recent open Incident for this monitor.
        open_incident = await _latest_open_incident(session, monitor.id)
        if open_incident is not None:
            open_incident.closed_at = datetime.now(UTC)
            open_incident.close_reason = result_reason
            open_incident.outcome = "recovered"
            await session.flush()
            await _maybe_alert(
                alert_sink,
                AlertIntent(monitor.id, action, open_incident.id, result_reason),
                monitor,
            )
            # Post-incident Summary (ADR-0006 kind #3): a structured follow-up
            # richer than the one-line recovery Alert, sent after the recovery
            # Alert via the same sink. Only sent once (summary_sent_at guard).
            if open_incident.summary_sent_at is None and alert_sink is not None:
                open_incident.failed_check_count = await _count_failed_checks(
                    session, monitor.id, open_incident.opened_at, open_incident.closed_at
                )
                open_incident.summary_sent_at = datetime.now(UTC)
                await session.flush()
    elif action is Action.FLAP_START:
        await _maybe_alert(
            alert_sink, AlertIntent(monitor.id, action, None, "flapping detected"), monitor
        )
    elif action is Action.FLAP_END:
        await _maybe_alert(
            alert_sink, AlertIntent(monitor.id, action, None, "flapping resolved"), monitor
        )

    return action


async def _latest_open_incident(session: AsyncSession, monitor_id: int) -> Incident | None:
    stmt = (
        select(Incident)
        .where(Incident.monitor_id == monitor_id, Incident.closed_at.is_(None))
        .order_by(Incident.opened_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _count_failed_checks(
    session: AsyncSession, monitor_id: int, opened_at: datetime, closed_at: datetime | None
) -> int:
    """Count failed Checks during an Incident span (for the post-incident summary)."""
    end = closed_at or datetime.now(UTC)
    stmt = (
        select(func.count())
        .select_from(Check)
        .where(
            Check.monitor_id == monitor_id,
            Check.checked_at >= opened_at,
            Check.checked_at <= end,
            ~Check.success,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def _maybe_alert(sink: AlertSink | None, intent: AlertIntent, monitor: Monitor) -> None:
    if sink is None:
        return
    # Mute is a delivery-layer concern: a muted Monitor still opens/closes
    # Incidents (already persisted above); mute suppresses only the Alert send.
    if monitor.muted:
        log.info("monitor %s is muted; suppressing alert %s", monitor.id, intent.action)
        return
    try:
        await sink(intent)
    except Exception:
        log.exception("alert sink failed for monitor %s action %s", monitor.id, intent.action)
