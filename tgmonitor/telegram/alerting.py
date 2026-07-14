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

from tgmonitor.incidents import Action
from tgmonitor.models import Monitor, User
from tgmonitor.telegram.client import NotificationChannel
from tgmonitor.worker.alerting import AlertIntent, AlertSink

log = logging.getLogger("tgmonitor.telegram.alerting")


def format_alert(intent: AlertIntent, monitor_name: str) -> str | None:
    """Format an Alert message for one transition. Returns None for actions
    that produce no Alert (Action.NONE).

    One message per transition: opened, recovered, flap start, flap end.
    """
    if intent.action is Action.OPEN_INCIDENT:
        return f"🔴 <b>{monitor_name}</b> is down\n{intent.reason}"
    if intent.action is Action.CLOSE_INCIDENT:
        return f"🟢 <b>{monitor_name}</b> recovered\n{intent.reason}"
    if intent.action is Action.FLAP_START:
        return f"🟡 <b>{monitor_name}</b> is flapping — suppressing further alerts"
    if intent.action is Action.FLAP_END:
        return f"🟢 <b>{monitor_name}</b> stabilized — resuming normal alerting"
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
        from tgmonitor.db import session_factory as sf

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
            await ch.send(user.telegram_chat_id, text)

    return sink
