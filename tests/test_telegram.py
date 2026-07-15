"""Tests for the Telegram integration's pure seams: alert formatting and
command extraction. The Bot API client itself is behind a thin httpx boundary
and is not unit-tested (the spec mandates a fake at the delivery layer only).
"""

from __future__ import annotations

from tgmonitor.incidents import Action
from tgmonitor.telegram.alerting import format_alert
from tgmonitor.telegram.webhook import _extract_command
from tgmonitor.worker.alerting import AlertIntent


def _intent(action: Action, reason: str = "x") -> AlertIntent:
    return AlertIntent(monitor_id=1, action=action, incident_id=None, reason=reason)


def test_format_open_incident() -> None:
    msg = format_alert(_intent(Action.OPEN_INCIDENT, "HTTP 500"), "Billing API")
    assert msg is not None
    assert "Billing API" in msg
    assert "down" in msg
    assert "HTTP 500" in msg


def test_format_close_incident() -> None:
    msg = format_alert(_intent(Action.CLOSE_INCIDENT, "ok"), "Billing API")
    assert msg is not None
    assert "recovered" in msg
    assert "Billing API" in msg


def test_format_flap_start() -> None:
    msg = format_alert(_intent(Action.FLAP_START), "Flaky")
    assert msg is not None
    assert "flapping" in msg.lower()


def test_format_flap_end() -> None:
    msg = format_alert(_intent(Action.FLAP_END), "Flaky")
    assert msg is not None
    assert "stabilized" in msg.lower()


def test_format_none_action_returns_none() -> None:
    assert format_alert(_intent(Action.NONE), "X") is None


def test_format_escapes_markup_in_monitor_name() -> None:
    """A monitor name with HTML markup must not break parse_mode=HTML (#28)."""
    msg = format_alert(_intent(Action.OPEN_INCIDENT, "HTTP 500"), "a<b")
    assert msg is not None
    assert "a&lt;b" in msg
    assert "a<b>" not in msg  # the raw <b> would open an entity


def test_format_escapes_markup_in_reason() -> None:
    """A reason containing markup (e.g. a body_contains keyword) must be escaped."""
    msg = format_alert(_intent(Action.OPEN_INCIDENT, '<div id="app">'), "Billing")
    assert msg is not None
    assert "<div" not in msg
    assert "&lt;div" in msg


def test_escape_helper() -> None:
    """The shared HTML escape helper covers the characters Telegram parses."""
    from tgmonitor.telegram.alerting import _esc

    assert _esc("a<b") == "a&lt;b"
    assert _esc("a>b") == "a&gt;b"
    assert _esc("a&b") == "a&amp;b"
    assert _esc('<div id="x">') == "&lt;div id=&quot;x&quot;&gt;"


def test_extract_command_basic() -> None:
    assert _extract_command("/start abc123") == ("start", "abc123")


def test_extract_command_no_arg() -> None:
    assert _extract_command("/help") == ("help", "")


def test_extract_command_with_bot_suffix() -> None:
    assert _extract_command("/status@MyBot") == ("status", "")


def test_extract_command_empty() -> None:
    assert _extract_command(None) == ("", "")
    assert _extract_command("") == ("", "")


def test_extract_command_case_insensitive() -> None:
    assert _extract_command("/START token") == ("start", "token")
