"""Inbound Telegram command handlers (ADR-0007).

Each command is a function that takes the resolved User + args + a session and
returns the reply text. They are idempotent (safe under Telegram redelivery):
/mute twice = still muted; /status re-renders the same snapshot.

Commands authenticate by resolving the incoming chat_id to a User (ADR-0007).
The webhook does that resolution; these handlers assume an authenticated User.

Vocabulary (CONTEXT.md): /status returns an on-demand Report; /mute toggles the
delivery-suppression flag (Checks keep running, Incidents tracked).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.incident_model import Incident
from tgmonitor.models import Check, Monitor
from tgmonitor.telegram.html import esc_html

# Shared HTML-escape helper for parse_mode=HTML messages (#28).
_esc = esc_html


HELP_TEXT = (
    "<b>TelegramMonitor commands</b>\n\n"
    "/status — live state of all your monitors\n"
    "/mute &lt;name|all&gt; — suppress alerts for a monitor (checks keep running)\n"
    "/unmute &lt;name|all&gt; — resume alert delivery\n"
    "/incidents — your recent incidents\n"
    "/help — this message"
)


async def cmd_help() -> str:
    return HELP_TEXT


def _classify_monitor(monitor: Monitor, latest: Check | None) -> str:
    """Classify a Monitor for /status, consistent with the dashboard badge (#33).

    - ``paused`` takes precedence (no execution, no fresh results).
    - ``incident_status`` is authoritative: ``down``/``flapping`` come from the
      state machine, so /status agrees with the dashboard instead of guessing
      from a single stale Check.
    - A Monitor with zero Checks is ``unknown``, not silently ``up``.
    - A paused Monitor is not reported ``down`` on a stale failed Check.
    """
    if monitor.paused:
        return "paused"
    if monitor.incident_status == "flapping":
        return "flapping"
    if monitor.incident_status == "down":
        return "down"
    if latest is None:
        return "unknown"
    return "up" if latest.success else "down"


async def cmd_status(session: AsyncSession, user_id: int) -> str:
    """On-demand snapshot of all the user's Monitors (must be sub-second).

    Reads the latest Check per monitor in one batched query and renders an
    all-up summary with per-monitor state + reason. State is derived from
    ``incident_status`` (the authoritative state-machine value) plus the latest
    Check, so it matches the dashboard (#33).
    """
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
        return "You have no monitors yet. Add one via the web UI."

    ids = [m.id for m in monitors]
    # Latest check per monitor via a max(checked_at) subquery join.
    from sqlalchemy import func

    max_subq = (
        select(Check.monitor_id.label("mid"), func.max(Check.checked_at).label("mcat"))
        .where(Check.monitor_id.in_(ids))
        .group_by(Check.monitor_id)
        .subquery()
    )
    latest = {
        c.monitor_id: c
        for c in (
            await session.execute(
                select(Check).join(
                    max_subq,
                    (Check.monitor_id == max_subq.c.mid) & (Check.checked_at == max_subq.c.mcat),
                )
            )
        ).scalars()
    }

    # Anything not "up" gets a line so the user sees paused/unknown/flapping/down.
    flagged = [
        (m, _classify_monitor(m, latest.get(m.id)))
        for m in monitors
        if _classify_monitor(m, latest.get(m.id)) != "up"
    ]
    if not flagged:
        return f"✅ All {len(monitors)} monitor(s) are up."
    lines = [f"⚠️ {len(flagged)}/{len(monitors)} monitor(s) need attention:"]
    for m, state in flagged:
        c = latest.get(m.id)
        reason = _esc(c.reason) if c and not c.success else ""
        tail = f" — {reason}" if reason else ""
        lines.append(f"• <b>{_esc(m.name)}</b> [{state}]{tail}")
    return "\n".join(lines)


async def cmd_mute(session: AsyncSession, user_id: int, arg: str) -> str:
    """Suppress Alert delivery for a Monitor (Checks keep running). Idempotent."""
    if not arg:
        return "Usage: /mute <monitor name | all>"
    monitors = (
        (
            await session.execute(
                select(Monitor).where(Monitor.user_id == user_id).order_by(Monitor.id)
            )
        )
        .scalars()
        .all()
    )
    if arg.lower() == "all":
        for m in monitors:
            m.muted = True
        await session.commit()
        return f"🔇 Muted all {len(monitors)} monitor(s)."
    # Match by name (case-insensitive substring).
    matches = [m for m in monitors if arg.lower() in m.name.lower()]
    if not matches:
        return f"No monitor matching '{arg}'."
    for m in matches:
        m.muted = True
    await session.commit()
    names = ", ".join(_esc(m.name) for m in matches)
    return f"🔇 Muted: {names}"


async def cmd_unmute(session: AsyncSession, user_id: int, arg: str) -> str:
    """Resume Alert delivery. Idempotent."""
    if not arg:
        return "Usage: /unmute <monitor name | all>"
    monitors = (
        (
            await session.execute(
                select(Monitor).where(Monitor.user_id == user_id).order_by(Monitor.id)
            )
        )
        .scalars()
        .all()
    )
    if arg.lower() == "all":
        for m in monitors:
            m.muted = False
        await session.commit()
        return f"🔔 Unmuted all {len(monitors)} monitor(s)."
    matches = [m for m in monitors if arg.lower() in m.name.lower()]
    if not matches:
        return f"No monitor matching '{arg}'."
    for m in matches:
        m.muted = False
    await session.commit()
    names = ", ".join(_esc(m.name) for m in matches)
    return f"🔔 Unmuted: {names}"


async def cmd_incidents(session: AsyncSession, user_id: int) -> str:
    """List the user's recent Incidents across all their monitors."""
    monitors = (
        (await session.execute(select(Monitor.id).where(Monitor.user_id == user_id)))
        .scalars()
        .all()
    )
    if not monitors:
        return "You have no monitors yet."
    stmt = (
        select(Incident)
        .where(Incident.monitor_id.in_(monitors))
        .order_by(Incident.opened_at.desc())
        .limit(10)
    )
    incidents = (await session.execute(stmt)).scalars().all()
    if not incidents:
        return "No incidents recorded."
    lines = ["<b>Recent incidents:</b>"]
    for inc in incidents:
        state = "🔴 open" if inc.closed_at is None else "🟢 closed"
        mon = await session.get(Monitor, inc.monitor_id)
        name = _esc(mon.name) if mon else f"#{inc.monitor_id}"
        lines.append(
            f"• {state} <b>{name}</b> — {_esc(inc.open_reason)} ({inc.opened_at:%Y-%m-%d %H:%M})"
        )
    return "\n".join(lines)


def unknown_command_reply() -> str:
    return "Unknown command. Try /help for the list of commands."
