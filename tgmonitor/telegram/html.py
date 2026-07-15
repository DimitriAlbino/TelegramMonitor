"""HTML-escape helper for Telegram parse_mode=HTML messages (#28).

User-controlled strings (Monitor names, Result reasons) interpolated into
HTML-mode Telegram messages must be escaped — a stray ``<`` makes the Bot API
reject the message with 400 "can't parse entities", so the Alert never delivers.
This is the single shared helper the alerting, command, and report formatters
all use.
"""

from __future__ import annotations

from html import escape


def esc_html(text: str) -> str:
    """Escape user-controlled text for interpolation into a parse_mode=HTML message."""
    return escape(text, quote=True)
