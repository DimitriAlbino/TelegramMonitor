"""Telegram integration: Bot API client, Notification Channel, webhook, alerting.

CONTEXT.md vocabulary: the message is an ``Alert`` (one per Incident transition);
the destination is a ``Notification Channel`` (resolves to shared bot + chat_id).
"""

from __future__ import annotations

from .client import NotificationChannel, TelegramClient

__all__ = ["NotificationChannel", "TelegramClient"]
