"""Outbound email for account verification and password reset.

In dev (no SMTP configured), messages are logged to stdout instead of sent —
the verification/reset links appear in the logs, so the flow is testable
without a mail server. In prod, a real SMTP connection is used.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from tgmonitor.config import get_settings

log = logging.getLogger("tgmonitor.auth.email")


def _build_link(path: str, token: str) -> str:
    return f"{get_settings().public_base_url}{path}?token={token}"


def send_verification_email(to_email: str, token: str) -> None:
    link = _build_link("/auth/verify", token)
    body = (
        "Welcome to TelegramMonitor.\n\n"
        "Verify your email by visiting this link:\n"
        f"{link}\n\n"
        "If you didn't sign up, you can ignore this email."
    )
    _send(to_email, "Verify your TelegramMonitor account", body)


def send_reset_email(to_email: str, token: str) -> None:
    link = _build_link("/auth/reset", token)
    body = (
        "Reset your TelegramMonitor password by visiting this link:\n"
        f"{link}\n\n"
        "If you didn't request a reset, you can ignore this email."
    )
    _send(to_email, "Reset your TelegramMonitor password", body)


def _send(to_email: str, subject: str, body: str) -> None:
    settings = get_settings()
    if not settings.smtp_host:
        # Dev mode: print the link so the flow is testable without SMTP.
        log.warning(
            "SMTP not configured; printing email instead of sending.\n%s\n%s", subject, body
        )
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to_email
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
        if settings.smtp_username:
            server.starttls()
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)
