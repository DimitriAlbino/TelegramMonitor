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


def test_status_item_never_exposes_target() -> None:
    """The public status-page entry omits the raw target (#33/#44).

    Behavioral: build the actual entry the JSON view emits and assert the origin
    URL is absent while the display fields are present. This exercises the real
    item builder (_render_page's per-monitor payload), not the state shape.
    """
    from tgmonitor.api.statuspage import _status_item

    class _Monitor:
        name = "Public site"
        check_kind = "http"
        target = "https://internal.origin.example/secret-path"

    item = _status_item(_Monitor(), latest=None, incidents=[])
    assert "target" not in item, "the public JSON must not leak the monitor target"
    assert item["name"] == "Public site"
    assert item["check_kind"] == "http"
    assert item["state"] == "unknown"
    # The target value must not appear anywhere in the serialized entry.
    assert "internal.origin.example" not in str(item)


# --- pagination (#33) ---


async def test_list_monitors_applies_offset_to_query() -> None:
    """list_monitors renders an OFFSET into its query so old rows are reachable (#33)."""
    from tgmonitor.api.monitors import list_monitors

    class _Res:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Session:
        def __init__(self):
            self.stmts: list[str] = []

        async def execute(self, stmt):
            self.stmts.append(str(stmt))
            return _Res()

    class _User:
        id = 1

    session = _Session()
    await list_monitors(_User(), session, limit=50, offset=25)
    assert any("OFFSET" in s.upper() for s in session.stmts), (
        "the monitors query must apply the requested offset"
    )
