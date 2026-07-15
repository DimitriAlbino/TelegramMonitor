"""Tests for the API/UI correctness cleanup (#33).

Covers:
- api_content Monitor creation rejected without json_field_path/json_keyword.
- _classify_monitor edge states (zero-check, paused, incident_status).
- Public status JSON no longer exposes the raw target.
- List endpoints support offset pagination.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tgmonitor.api.monitors import MonitorCreate
from tgmonitor.api.statuspage import _monitor_state
from tgmonitor.telegram.commands import _classify_monitor

# --- api_content creation validation (#33) ---


def test_api_content_requires_json_fields() -> None:
    with pytest.raises(ValidationError):
        MonitorCreate(
            name="x",
            check_kind="api_content",
            target="https://api.example.com/status",
            interval_s=30,
        )


def test_api_content_requires_both_json_fields() -> None:
    with pytest.raises(ValidationError):
        MonitorCreate(
            name="x",
            check_kind="api_content",
            target="https://api.example.com/status",
            interval_s=30,
            json_field_path="mode",  # missing json_keyword
        )


def test_api_content_valid_when_complete() -> None:
    m = MonitorCreate(
        name="x",
        check_kind="api_content",
        target="https://api.example.com/status",
        interval_s=30,
        json_field_path="mode",
        json_keyword="stale",
    )
    assert m.json_field_path == "mode"


# --- _classify_monitor edge states (#33) ---


class _Mon:
    def __init__(self, *, paused=False, status="ok") -> None:
        self.paused = paused
        self.incident_status = status


class _Check:
    def __init__(self, success=True, reason="ok") -> None:
        self.success = success
        self.reason = reason


def test_zero_checks_is_unknown_not_up() -> None:
    assert _classify_monitor(_Mon(), None) == "unknown"


def test_paused_takes_precedence() -> None:
    assert _classify_monitor(_Mon(paused=True, status="down"), _Check(False, "x")) == "paused"


def test_incident_status_down_is_down() -> None:
    assert _classify_monitor(_Mon(status="down"), _Check(True, "ok")) == "down"


def test_incident_status_flapping_is_flapping() -> None:
    assert _classify_monitor(_Mon(status="flapping"), None) == "flapping"


def test_ok_with_successful_check_is_up() -> None:
    assert _classify_monitor(_Mon(), _Check(True)) == "up"


# --- public status JSON target leak (#33) ---


def test_monitor_state_does_not_include_target() -> None:
    # _render_page builds items from name + check_kind + _monitor_state(latest);
    # _monitor_state itself must not surface the target. (The target removal is
    # asserted structurally at the items level; here we pin the state shape.)
    state = _monitor_state(None)
    assert "target" not in state
    assert state["state"] == "unknown"


# --- pagination (#33) ---


def test_list_endpoints_accept_offset() -> None:
    """Structural check: the list handlers declare an offset query param."""
    import inspect

    from tgmonitor.api.monitors import list_incidents, list_monitors

    for fn in (list_monitors, list_incidents):
        params = inspect.signature(fn).parameters
        assert "offset" in params, f"{fn.__name__} must support offset pagination"
