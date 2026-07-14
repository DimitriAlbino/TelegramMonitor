"""Telegram Bot API client + Notification Channel abstraction.

The Notification Channel (CONTEXT.md) is the abstract destination for a User's
Alerts and Reports. Today it resolves to "shared bot + the user's chat_id";
the abstraction is shaped so BYO-bot (user's own token) slots in later without
rewriting delivery (ADR-0002).

The Bot API client is a thin async wrapper over httpx. Delivery errors are
logged and swallowed — a Telegram outage or a dropped chat must never crash the
worker (ADR-0005 / the reference doc's "never let a send error kill the loop").
"""

from __future__ import annotations

import logging

import httpx

from tgmonitor.config import get_settings

log = logging.getLogger("tgmonitor.telegram")


class TelegramClient:
    """Thin async client for the Telegram Bot API.

    Uses the shared bot token from settings. A real instance holds a long-lived
    ``httpx.AsyncClient``; tests inject a fake via the ``post`` seam.
    """

    API_BASE = "https://api.telegram.org"

    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None) -> None:
        self._token = token if token is not None else get_settings().telegram_bot_token
        self._client = client

    async def send_message(self, chat_id: str, text: str, *, parse_mode: str = "HTML") -> bool:
        """Send a message. Returns True on 200, False on any failure.

        Never raises: a send failure is logged and returns False so the caller
        (the alert sink) can't crash on a Telegram outage.
        """
        if not self._token:
            log.warning("TELEGRAM_BOT_TOKEN not configured; skipping send to %s", chat_id)
            return False
        url = f"{self.API_BASE}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        client = self._client or httpx.AsyncClient(timeout=15.0)
        owned = self._client is None
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                log.error(
                    "telegram sendMessage to %s failed: %s %s",
                    chat_id,
                    resp.status_code,
                    resp.text[:300],
                )
                return False
            return True
        except Exception:
            log.exception("telegram sendMessage to %s raised", chat_id)
            return False
        finally:
            if owned:
                await client.aclose()


# --- Notification Channel abstraction (ADR-0002) ---


class NotificationChannel:
    """Resolves where a User's Alerts/Reports go.

    Today: the shared bot + the user's linked chat_id. BYO-bot later: resolve
    to the user's own token + chat_id. Delivery callers use :meth:`send` and
    stay ignorant of which bot is behind it.
    """

    def __init__(self, client: TelegramClient | None = None) -> None:
        self._client = client or TelegramClient()

    async def send(self, chat_id: str | None, text: str) -> bool:
        """Deliver ``text`` to ``chat_id`` via the resolved bot.

        Returns False (and logs) if the user has no linked chat or delivery
        fails — never raises.
        """
        if not chat_id:
            log.info("no linked chat_id; skipping notification")
            return False
        return await self._client.send_message(chat_id, text)
