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
from collections.abc import Callable
from datetime import UTC, datetime

from tgmonitor.incidents import Action
from tgmonitor.models import Monitor, User
from tgmonitor.telegram.client import NotificationChannel
from tgmonitor.telegram.html import esc_html
from tgmonitor.worker.alerting import AlertIntent, AlertSink

log = logging.getLogger("tgmonitor.telegram.alerting")

# Shared HTML-escape helper for parse_mode=HTML messages (#28).
_esc = esc_html


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
    *,
    now: Callable[[], datetime] | None = None,
) -> AlertSink:
    """Build the async alert_sink callable the CheckEngine wants.

    The sink resolves the Monitor's owning User's chat_id and delivers the
    formatted Alert. It opens its own short-lived session (the engine's session
    is already committed by the time the sink runs).

    ``now`` is the clock provider (defaults to real UTC) so the quiet-hours
    decision is testable without a wall clock.
    """

    ch = channel or NotificationChannel()
    clock = now or (lambda: datetime.now(UTC))

    async def sink(intent: AlertIntent) -> None:
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
            # when inside the window; critical Monitors page immediately. When
            # deferred, do NOT send the post-incident summary either (#21): it
            # would page during quiet hours, and stamping summary_sent_at would
            # record it as sent with no retry.
            deferred = should_defer(
                now_utc=clock(),
                start_hhmm=user.quiet_hours_start,
                end_hhmm=user.quiet_hours_end,
                tz_name=user.quiet_hours_tz,
                critical=monitor.critical,
            )
            if deferred:
                log.info(
                    "deferring non-critical alert for monitor %s (quiet hours)", intent.monitor_id
                )
                _queue_digest(user.id, text)
                return
            delivered = await ch.send(user.telegram_chat_id, text)
            # Post-incident Summary (ADR-0006 kind #3): a structured follow-up
            # distinct from the recovery one-liner. Send it only after the
            # recovery Alert actually went out, and only stamp summary_sent_at
            # after a confirmed summary send (#21) — never pre-stamp. (Mute is
            # already honoured upstream: deliver_alerts does not call the sink
            # for a muted monitor, so we never reach here when muted.)
            if (
                delivered
                and intent.action is Action.CLOSE_INCIDENT
                and intent.incident_id is not None
            ):
                from tgmonitor.incident_model import Incident
                from tgmonitor.reports import render_post_incident_summary

                inc = await session.get(Incident, intent.incident_id)
                if inc is not None and inc.summary_sent_at is None:
                    summary = await render_post_incident_summary(session, inc)
                    if await ch.send(user.telegram_chat_id, summary):
                        inc.summary_sent_at = clock()
                        await session.commit()

    return sink


# --- In-process digest queue (single-VPS launch envelope) ---
# Keyed by user id; each value is a list of deferred Alert texts. Flushed by the
# worker's tick loop when the quiet window ends (T6's quiet-hours flusher).
import asyncio  # noqa: E402

from tgmonitor.telegram.quiet_hours import is_in_quiet_window  # noqa: E402

_digest_lock = asyncio.Lock()
_digests: dict[int, list[str]] = {}

# Telegram rejects messages longer than 4096 chars with a 400 (#40); a digest
# joined from many deferred alerts can exceed that, so it is split into chunks.
TELEGRAM_MAX_CHARS = 4096
# Bound the per-user queue so a noisy monitor during a long quiet window cannot
# grow it without limit (#40); oldest deferred alerts are dropped past the cap.
MAX_QUEUED_PER_USER = 200


def _queue_digest(user_id: int, text: str) -> None:
    """Append a deferred Alert to the user's digest queue, bounded (#40)."""
    q = _digests.setdefault(user_id, [])
    q.append(text)
    if len(q) > MAX_QUEUED_PER_USER:
        dropped = len(q) - MAX_QUEUED_PER_USER
        del q[:dropped]
        log.warning(
            "digest queue for user %s exceeded %d; dropped %d oldest deferred alert(s)",
            user_id,
            MAX_QUEUED_PER_USER,
            dropped,
        )


def _tg_len(s: str) -> int:
    """Length as Telegram counts it: UTF-16 code units (#40).

    Telegram's 4096 limit is in UTF-16 units, so an emoji (astral char) counts as
    2, not 1. Measuring with ``len`` would under-count an emoji-heavy body and let
    it slip over the real limit.
    """
    return len(s.encode("utf-16-le")) // 2


def _truncate_tg(s: str, budget: int) -> str:
    """Truncate ``s`` to at most ``budget`` UTF-16 units, ending with an ellipsis."""
    if _tg_len(s) <= budget:
        return s
    s = s[: budget - 1]
    while _tg_len(s) > budget - 1:  # trim astral chars that count double
        s = s[:-1]
    return s + "…"


def _chunk_message_groups(messages: list[str], header: str, limit: int) -> list[list[str]]:
    """Split messages into groups whose rendered body stays within ``limit`` (#40).

    Each group renders as ``header`` + blank-line-joined messages. Lengths are
    measured in UTF-16 units (Telegram's unit). A single message longer than the
    budget is truncated so it can never wedge delivery.
    """
    sep = "\n\n"
    base = _tg_len(header) + _tg_len(sep)  # header plus the separator before message 1
    groups: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for m in messages:
        mt = _truncate_tg(m, limit - base)
        add = _tg_len(mt) + (_tg_len(sep) if cur else 0)
        if cur and base + cur_len + add > limit:
            groups.append(cur)
            cur, cur_len = [], 0
            add = _tg_len(mt)
        cur.append(mt)
        cur_len += add
    if cur:
        groups.append(cur)
    return groups


def queued_user_ids() -> list[int]:
    """Snapshot of user ids with pending deferred digests (for the flusher)."""
    return list(_digests.keys())


async def flush_digest(user_id: int) -> list[str]:
    """Pop and return the user's queued digest messages. Empty if none."""
    async with _digest_lock:
        return _digests.pop(user_id, [])


async def flush_due_digests(
    channel: NotificationChannel | None = None,
    *,
    now_utc: datetime | None = None,
    user_lookup: UserLookup | None = None,
) -> int:
    """Deliver deferred (non-critical) digests whose quiet window has ended.

    Called from the worker's tick loop each tick (#20): for every user with a
    queued digest, if they are no longer inside their quiet window, pop the
    queued messages and send them as one combined digest. Returns the number of
    digests delivered. A user still inside their window is left queued (no drop).

    The queue is bounded by delivery — once flushed the entry is removed, so it
    cannot grow unbounded across the window.

    ``now_utc`` and ``user_lookup`` are injectable so the decision is testable
    without the DB or a wall clock (defaults: real time + a DB-backed lookup).
    """
    ch = channel or NotificationChannel()
    clock = now_utc if now_utc is not None else datetime.now(UTC)
    lookup = user_lookup or _DbUserLookup()
    delivered = 0
    for user_id in queued_user_ids():
        messages = await flush_digest(user_id)
        if not messages:
            continue
        user = await lookup.get(user_id)
        # Re-check the window at delivery time: a user may still be inside it
        # (queued by a different tick). Leave their messages queued in that case
        # rather than dropping or double-sending.
        if user is not None and is_in_quiet_window(
            clock, user.quiet_hours_start, user.quiet_hours_end, user.quiet_hours_tz
        ):
            _digests.setdefault(user_id, []).extend(messages)  # re-queue, skip
            continue
        chat_id = user.telegram_chat_id if user else None
        if not chat_id:
            continue  # no linked chat; drop (the alert was non-critical)
        header = "📨 Quiet-hours digest (deferred alerts):"
        groups = _chunk_message_groups(messages, header, TELEGRAM_MAX_CHARS)
        sent_all = True
        for i, group in enumerate(groups):
            body = header + "\n\n" + "\n\n".join(group)
            if not await ch.send(chat_id, body):
                # Send failed: re-queue this chunk and everything after it,
                # oldest-first, so nothing is silently dropped (#40). Stop here.
                remaining = [m for g in groups[i:] for m in g]
                _digests[user_id] = remaining + _digests.get(user_id, [])
                sent_all = False
                break
        if sent_all:
            delivered += 1
    return delivered


# A user-lookup seam so the flusher is testable without the DB. The default
# implementation reads the User row from Postgres.
class UserLookup:
    """Abstract user lookup for the digest flusher."""

    async def get(self, user_id: int) -> User | None:  # pragma: no cover - abstract
        raise NotImplementedError


class _DbUserLookup(UserLookup):
    async def get(self, user_id: int) -> User | None:
        from tgmonitor.db import session_factory as sf

        async with sf()() as session:
            return await session.get(User, user_id)
