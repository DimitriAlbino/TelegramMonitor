"""Tests for the Monitor config-building helper used by the web UI (#23).

The edit handler used to rebuild ``config`` from scratch with only the timeout
fields plus HTTP knobs, dropping follow_redirects / json_field_path /
json_keyword even though the form posts them. Renaming an api_content monitor
wiped its JSON fields, so every subsequent Check failed and a false Incident
fired; HTTP follow-redirects silently reverted on any edit.

The helper now merges posted fields over the existing config so a round-trip
edit (change only the name) leaves the effective check behavior unchanged.
"""

from __future__ import annotations

from tgmonitor.ui.app_routes import _build_monitor_config


def test_edit_api_content_preserves_json_fields() -> None:
    """Renaming an api_content monitor must not wipe json_field_path/keyword.

    The edit form re-posts the current JSON fields (pre-populated), so the
    round-trip preserves them instead of dropping them as the old rebuild did.
    """
    existing = {"json_field_path": "mode", "json_keyword": "stale", "timeout_s": 10.0}
    cfg = _build_monitor_config(
        "api_content",
        timeout_s=10.0,
        expected_status=200,
        body_contains="",
        max_latency_ms=None,
        follow_redirects=False,
        json_field_path="mode",  # re-posted from the pre-populated form
        json_keyword="stale",
        existing=existing,
    )
    assert cfg["json_field_path"] == "mode"
    assert cfg["json_keyword"] == "stale"


def test_edit_http_preserves_follow_redirects() -> None:
    """Editing an HTTP monitor must keep follow_redirects and other knobs."""
    existing = {
        "expected_status": 200,
        "follow_redirects": True,
        "body_contains": "alive",
        "timeout_s": 5.0,
    }
    cfg = _build_monitor_config(
        "http",
        timeout_s=5.0,
        expected_status=200,
        body_contains="alive",
        max_latency_ms=None,
        follow_redirects=True,
        json_field_path="",
        json_keyword="",
        existing=existing,
    )
    assert cfg["follow_redirects"] is True
    assert cfg["body_contains"] == "alive"


def test_round_trip_name_only_edit_leaves_behavior_unchanged() -> None:
    """Changing only the name (re-posting the same knobs) yields identical config."""
    existing = {"json_field_path": "data.status", "json_keyword": "down", "timeout_s": 10.0}
    cfg = _build_monitor_config(
        "api_content",
        timeout_s=10.0,
        expected_status=200,
        body_contains="",
        max_latency_ms=None,
        follow_redirects=False,
        json_field_path="data.status",
        json_keyword="down",
        existing=existing,
    )
    assert cfg == {"json_field_path": "data.status", "json_keyword": "down", "timeout_s": 10.0,
                    "tcp_timeout_s": 10.0}


def test_create_builds_from_scratch() -> None:
    """Create (existing=None) builds a fresh config from the posted fields."""
    cfg = _build_monitor_config(
        "http",
        timeout_s=8.0,
        expected_status=204,
        body_contains="ok",
        max_latency_ms=500,
        follow_redirects=True,
        json_field_path="",
        json_keyword="",
    )
    assert cfg == {
        "timeout_s": 8.0,
        "tcp_timeout_s": 8.0,
        "expected_status": 204,
        "follow_redirects": True,
        "body_contains": "ok",
        "max_latency_ms": 500,
    }


def test_edit_http_clears_blank_body_contains() -> None:
    """Clearing an optional knob (empty string) removes it from config."""
    existing = {"expected_status": 200, "body_contains": "old", "timeout_s": 10.0}
    cfg = _build_monitor_config(
        "http",
        timeout_s=10.0,
        expected_status=200,
        body_contains="",  # user cleared it
        max_latency_ms=None,
        follow_redirects=False,
        json_field_path="",
        json_keyword="",
        existing=existing,
    )
    assert "body_contains" not in cfg
