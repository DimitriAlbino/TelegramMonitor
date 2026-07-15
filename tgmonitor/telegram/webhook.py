"""Telegram webhook receiver (ADR-0012).

``POST /telegram/webhook`` receives Updates Telegram pushes. The handler:
1. Validates the ``X-Telegram-Bot-Api-Secret-Token`` header (rejects spoofing).
2. Routes the command text (``/status``, ``/mute``, ``/unmute``,
   ``/incidents``, ``/help``).
3. Is idempotent — Telegram redelivers on non-2xx, so handlers must be safe to
   receive twice.

Account linking is done manually: the user enters their Telegram chat ID on the
Settings page (``/ui/settings``). The tokenized ``/start <token>`` flow was
removed (ADR-0002 superseded). ``/start`` now shows help and the user's chat ID,
pointing them to the Settings page. An unrecognized ``chat_id`` on any command
gets a "please link at /ui/settings" reply.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.config import get_settings
from tgmonitor.db import get_session
from tgmonitor.models import User
from tgmonitor.telegram.client import NotificationChannel

router = APIRouter(prefix="/telegram", tags=["telegram"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# A module-level channel so the webhook can reply inline.
_channel: NotificationChannel | None = None


def _get_channel() -> NotificationChannel:
    global _channel
    if _channel is None:
        _channel = NotificationChannel()
    return _channel


def _extract_command(text: str | None) -> tuple[str, str]:
    """Split ``/cmd arg`` → ("cmd", "arg"). arg is "" if absent."""
    if not text:
        return "", ""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lstrip("/").lower().split("@")[0]  # strip @botname suffix
    return cmd, (parts[1] if len(parts) > 1 else "")


async def _reply(chat_id: str, text: str) -> None:
    await _get_channel().send(chat_id, text)


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    session: SessionDep,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Receive a Telegram Update, validate the secret, and route the command."""
    secret = get_settings().telegram_webhook_secret
    # Reject mismatched/missing secrets (ADR-0012). Fail-closed if unconfigured.
    if not secret or x_telegram_bot_api_secret_token != secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid secret token")

    payload = await request.json()
    message = payload.get("message") or {}
    text = message.get("text")
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id")) if chat.get("id") else None
    if not chat_id:
        return {"status": "ignored"}  # not a message we can act on

    cmd, arg = _extract_command(text)

    if cmd == "start":
        await _handle_start(session, chat_id, arg)
        return {"status": "ok"}

    # All other commands require a linked chat. Unrecognized chat → "please link".
    user = await _user_for_chat(session, chat_id)
    if user is None:
        await _reply(
            chat_id,
            "I don't recognize this chat. To link your account, go to "
            f"{get_settings().public_base_url}/ui/settings and enter your "
            f"Telegram chat ID (it's {chat_id}).",
        )
        return {"status": "ok"}

    from tgmonitor.telegram import commands

    if cmd == "status":
        reply = await commands.cmd_status(session, user.id)
    elif cmd == "mute":
        reply = await commands.cmd_mute(session, user.id, arg)
    elif cmd == "unmute":
        reply = await commands.cmd_unmute(session, user.id, arg)
    elif cmd == "incidents":
        reply = await commands.cmd_incidents(session, user.id)
    elif cmd == "help":
        reply = await commands.cmd_help()
    elif cmd == "":
        return {"status": "ignored"}  # non-text message
    else:
        reply = commands.unknown_command_reply()
    await _reply(chat_id, reply)
    return {"status": "ok"}


async def _user_for_chat(session: AsyncSession, chat_id: str) -> User | None:
    result = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
    return result


async def _handle_start(session: AsyncSession, chat_id: str, arg: str) -> None:
    """Handle /start: show help and point to the Settings page.

    The tokenized deep-link binding flow was removed (ADR-0002 superseded).
    Users now link Telegram via the manual chat-ID field on the Settings page.
    ``/start`` with any argument is treated the same as bare ``/start``.
    """
    base = get_settings().public_base_url
    await _reply(
        chat_id,
        "Welcome to TelegramMonitor!\n\n"
        f"To link this chat to your account, go to {base}/ui/settings and "
        "enter your Telegram chat ID (yours is below) in the Telegram card.\n\n"
        f"Your chat ID: {chat_id}\n\n"
        "Once linked, I'll alert you when your monitors go down. "
        "Send /help for the command list.",
    )
