"""Telegram webhook receiver + account-linking flow (ADR-0012).

``POST /telegram/webhook`` receives Updates Telegram pushes. The handler:
1. Validates the ``X-Telegram-Bot-Api-Secret-Token`` header (rejects spoofing).
2. Routes the command text (``/start [token]`` for linking; other commands land
   in T6).
3. Is idempotent — Telegram redelivers on non-2xx, so handlers must be safe to
   receive twice (binding twice to the same chat is a no-op update).

Account linking (ADR-0002): the web UI generates a one-time deep-link token; the
user opens the bot via ``https://t.me/<bot>?start=<token>``; ``/start <token>``
binds the incoming ``chat_id`` to the User who generated the token.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.auth.dependencies import CurrentUser
from tgmonitor.auth.tokens import create_purpose_token, decode_token
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
    else:
        # Other commands (/status, /mute, /incidents, /help) land in T6. Until
        # then, reply with a pointer; an unlinked chat gets "please link".
        user = await _user_for_chat(session, chat_id)
        if user is None:
            await _reply(
                chat_id,
                "I don't recognize this chat. Please link your account at "
                f"{get_settings().public_base_url}",
            )
        else:
            await _reply(chat_id, "Commands like /status and /help arrive soon.")
    return {"status": "ok"}


async def _user_for_chat(session: AsyncSession, chat_id: str) -> User | None:
    result = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
    return result


async def _handle_start(session: AsyncSession, chat_id: str, arg: str) -> None:
    """Handle /start [link_token]: link the chat or show help."""
    if not arg:
        # Bare /start — help message pointing to the web UI.
        await _reply(
            chat_id,
            "Welcome to TelegramMonitor! Link your account by visiting "
            f"{get_settings().public_base_url} and using the 'Link Telegram' flow.\n"
            "Once linked, I'll alert you when your monitors go down.",
        )
        return

    # /start <link_token> — bind this chat to the account.
    payload = decode_token(arg, expected_purpose="link")
    if payload is None:
        await _reply(chat_id, "That link is invalid or expired. Please generate a new one.")
        return
    user = await session.get(User, int(payload.sub))
    if user is None:
        await _reply(chat_id, "Account not found. Please contact support.")
        return
    # Idempotent: binding twice to the same chat is a no-op. Binding a different
    # chat rebinds (the user re-linked from a new chat).
    user.telegram_chat_id = chat_id
    await session.commit()
    await _reply(chat_id, f"✅ Linked to {user.email}. You'll now receive alerts here.")


@router.get("/link-token")
async def create_link_token_endpoint(user: CurrentUser) -> dict[str, str]:
    """Generate a one-time deep-link token + the t.me URL for the user."""
    token, _ = create_purpose_token(user.id, "link", ttl_minutes=30)
    bot_name = get_settings().telegram_login_bot_name or "TelegramMonitorBot"
    deep_link = f"https://t.me/{bot_name}?start={token}"
    return {"token": token, "deep_link": deep_link}
