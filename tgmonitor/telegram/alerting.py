"""Bridge the worker's AlertIntent to Telegram delivery.

This is the real ``alert_sink`` the CheckEngine calls (T5 wired it as None; T4
supplies this). It resolves the Monitor → its owning User's linked chat_id and
formats an Alert message per Incident transition (ADR-0005: one opened-Alert,
one recovered-Alert per Incident; a single flap Alert).

The formatting is a pure function (``format_alert``) so it can be tested without
the channel. The sink resolves the chat and delivers; mute was already honoured
in the worker bridge (delivery layer, never the pure state machine).
"""

from __future__ import annotations

import logging
from html import escape

from tgmonitor.incidents import Action
from tgmonitor.models import Monitor, User
from tgmonitor.telegram.client import NotificationChannel
from tgmonitor.worker.alerting import AlertIntent, AlertSink

log = logging.getLogger("tgmonitor.telegram.alerting")


def _esc(text: str) -> str:
    """HTML-escape user-controlled text interpolated into parse_mode=HTML (#28).

    Telegram's HTML mode parses entities strictly; a stray ``<`` (a Monitor
    named ``a<b`` or a reason containing a ``body_contains`` keyword like
    ``<div id="app">``) makes the API reject the message with 400 "can't parse
    entities", so the Alert never delivers.
    """
    return escape(text, quote=True)


def format_alert(intent: AlertIntent, monitor_name: str) -> str | None:
    """Format an Alert message for one transition. Returns None for actions
    that produce no Alert (Action.NONE).

    One message per transition: opened, recovered, flap start, flap end.
    User-controlled strings (Monitor name, Result reason) are HTML-escaped (#28).
    """
    name = _esc(monitor_name)
    reason = _esc(intent.reason)
    if intent.action is Action.OPEN_INCIDENT:
        return f"🔴 <b>{name}</b> is down\n{reason}"
    if intent.action is Action.CLOSE_INCIDENT:
        return f"🟢 <b>{name}</b> recovered\n{reason}"
    if intent.action is Action.FLAP_START:
        return f"🟡 <b>{name}</b> is flapping — suppressing further alerts"
    if intent.action is Action.FLAP_END:
        return f"🟢 <b>{name}</b> stabilized — resuming normal alerting"
    return None


def make_alert_sink(
    channel: NotificationChannel | None = None,
) -> AlertSink:
    """Build the async alert_sink callable the CheckEngine wants.

    The sink resolves the Monitor's owning User's chat_id and delivers the
    formatted Alert. It opens its own short-lived session (the engine's session
    is already committed by the time the sink runs).
    """

    ch = channel or NotificationChannel()

    async def sink(intent: AlertIntent) -> None:
        from datetime import UTC, datetime

        from tgmonitor.db import session_factory as sf
        from tgmonitor.telegram.quiet_hours import should_defer

        async with sf()() as session:
            monitor = await session.get(Monitor, intent.monitor_id)
            if monitor is None:
                log.warning("alert for missing monitor %s", intent.monitor_id)
                return
            user = await session.get(User, monitor.user_id)
            if user is None or not user.telegram_chat_id:
                log.info("monitor %s owner has no linked chat; skipping alert", intent.monitor_id)
                return
            text = format_alert(intent, monitor.name)
            if text is None:
                return
            # Quiet hours (ADR-0005): defer non-critical Alerts into the digest
            # when inside the window; critical Monitors page immediately.
            now = datetime.now(UTC)
            if should_defer(
                now_utc=now,
                start_hhmm=user.quiet_hours_start,
                end_hhmm=user.quiet_hours_end,
                tz_name=user.quiet_hours_tz,
                critical=monitor.critical,
            ):
                log.info(
                    "deferring non-critical alert for monitor %s (quiet hours)", intent.monitor_id
                )
                _queue_digest(user.id, text)
                return
            await ch.send(user.telegram_chat_id, text)
            # Post-incident Summary (ADR-0006 kind #3): after a recovery Alert,
            # send a structured follow-up. The bridge already populated
            # failed_check_count and set summary_sent_at; render + send here.
            if intent.action is Action.CLOSE_INCIDENT and intent.incident_id is not None:
                from tgmonitor.incident_model import Incident
                from tgmonitor.reports import render_post_incident_summary

                inc = await session.get(Incident, intent.incident_id)
                if inc is not None and inc.summary_sent_at is not None:
                    summary = await render_post_incident_summary(session, inc)
                    await ch.send(user.telegram_chat_id, summary)

    return sink


# --- In-process digest queue (single-VPS launch envelope) ---
# Keyed by user id; each value is a list of deferred Alert texts. Flushed by the
# worker's tick loop when the quiet window ends (T6's quiet-hours flusher).
import asyncio  # noqa: E402

_digest_lock = asyncio.Lock()
_digests: dict[int, list[str]] = {}


def _queue_digest(user_id: int, text: str) -> None:
    """Append a deferred Alert to the user's digest queue (thread-safe-ish)."""
    _digests.setdefault(user_id, []).append(text)


async def flush_digest(user_id: int) -> list[str]:
    """Pop and return the user's queued digest messages. Empty if none."""
    async with _digest_lock:
        return _digests.pop(user_id, [])
