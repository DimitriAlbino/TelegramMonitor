"""Seam B tests — the api_content Check executor with a fake transport.

Mirrors the HTTP executor tests: keyword-absent → success, keyword-present →
failure, non-JSON → failure, missing field → failure, nested dot-path,
connection error, timeout.
"""

from __future__ import annotations

import httpx

from tests.conftest import FakeTransport, api_content_config
from tgmonitor.executors.api_content import run_api_content_check
from tgmonitor.executors.base import Transport, run_check


async def test_keyword_absent_is_success() -> None:
    body = '{"mode": "live"}'
    transport: Transport = FakeTransport(responses={"https://api.example.com/status": (200, body)})
    result = await run_api_content_check(api_content_config(), transport)
    assert result.success is True
    assert "mode" in result.reason


async def test_keyword_present_is_failure() -> None:
    body = '{"mode": "stale"}'
    transport: Transport = FakeTransport(responses={"https://api.example.com/status": (200, body)})
    result = await run_api_content_check(api_content_config(), transport)
    assert result.success is False
    assert "stale" in result.reason
    assert "mode" in result.reason


async def test_non_json_body_is_failure() -> None:
    transport: Transport = FakeTransport(
        responses={"https://api.example.com/status": (200, "<html>")}
    )
    result = await run_api_content_check(api_content_config(), transport)
    assert result.success is False
    assert "not valid JSON" in result.reason


async def test_missing_field_is_failure() -> None:
    body = '{"other": "value"}'
    transport: Transport = FakeTransport(responses={"https://api.example.com/status": (200, body)})
    result = await run_api_content_check(api_content_config(json_field_path="mode"), transport)
    assert result.success is False
    assert "not found" in result.reason


async def test_null_field_is_failure() -> None:
    body = '{"mode": null}'
    transport: Transport = FakeTransport(responses={"https://api.example.com/status": (200, body)})
    result = await run_api_content_check(api_content_config(), transport)
    assert result.success is False
    assert "null" in result.reason


async def test_nested_dot_path_success() -> None:
    body = '{"data": {"status": "healthy"}}'
    transport: Transport = FakeTransport(responses={"https://api.example.com/status": (200, body)})
    result = await run_api_content_check(
        api_content_config(json_field_path="data.status", json_keyword="stale"), transport
    )
    assert result.success is True


async def test_nested_dot_path_keyword_present_failure() -> None:
    body = '{"data": {"status": "stale"}}'
    transport: Transport = FakeTransport(responses={"https://api.example.com/status": (200, body)})
    result = await run_api_content_check(
        api_content_config(json_field_path="data.status", json_keyword="stale"), transport
    )
    assert result.success is False
    assert "stale" in result.reason


async def test_nested_dot_path_missing_intermediate_key() -> None:
    body = '{"other": {}}'
    transport: Transport = FakeTransport(responses={"https://api.example.com/status": (200, body)})
    result = await run_api_content_check(
        api_content_config(json_field_path="data.status"), transport
    )
    assert result.success is False
    assert "data" in result.reason


async def test_connection_error_is_failure() -> None:
    transport: Transport = FakeTransport(
        raise_on={"https://api.example.com/status": httpx.ConnectError("refused")}
    )
    result = await run_api_content_check(api_content_config(), transport)
    assert result.success is False
    assert "connection failed" in result.reason


async def test_timeout_is_failure() -> None:
    transport: Transport = FakeTransport(
        raise_on={"https://api.example.com/status": httpx.TimeoutException("read")}
    )
    result = await run_api_content_check(api_content_config(timeout_s=5.0), transport)
    assert result.success is False
    assert "timed out" in result.reason


async def test_dispatcher_runs_api_content_kind() -> None:
    body = '{"mode": "live"}'
    transport: Transport = FakeTransport(responses={"https://api.example.com/status": (200, body)})
    result = await run_check(api_content_config(), transport)
    assert result.success is True


def test_fake_transport_satisfies_protocol() -> None:
    assert isinstance(FakeTransport(), Transport)
