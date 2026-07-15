"""Seam B tests — the HTTP Check executor with a fake transport.

These assert what the executor *does* given transport outcomes, never touching
the network. Cases mirror the launch spec's Seam B list:
  200 → success; 500 → failure; 200 without expected keyword → failure;
  200 over max-latency → failure; connection refused → failure with reason;
  timed out → failure with reason.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import FakeTransport, http_config
from tgmonitor.executors.base import Transport, run_check
from tgmonitor.executors.http import run_http_check


@pytest.mark.parametrize(
    "status",
    [200, 201, 204, 301, 404],
)
async def test_expected_status_matches_and_succeeds(status: int) -> None:
    transport: Transport = FakeTransport(responses={"https://example.com": (status, "ok")})
    result = await run_http_check(
        http_config("https://example.com", expected_status=status), transport
    )
    assert result.success is True
    assert result.status_code == status
    assert result.reason == "ok"
    assert result.latency_ms is not None and result.latency_ms >= 0


async def test_unexpected_status_is_failure_with_reason() -> None:
    transport: Transport = FakeTransport(responses={"https://example.com": (500, "boom")})
    result = await run_http_check(http_config("https://example.com"), transport)
    assert result.success is False
    assert result.status_code == 500
    assert "expected 200" in result.reason


async def test_status_502_behind_200_proxy_detected() -> None:
    """User story #11: detect a semantic 502 by asserting the expected code."""
    transport: Transport = FakeTransport(responses={"https://api.example.com": (502, "")})
    result = await run_http_check(
        http_config("https://api.example.com", expected_status=200), transport
    )
    assert result.success is False
    assert result.status_code == 502


async def test_missing_body_keyword_is_failure() -> None:
    """User story #12: 200 but missing the required keyword → failure."""
    transport: Transport = FakeTransport(responses={"https://example.com": (200, "Database error")})
    result = await run_http_check(
        http_config("https://example.com", body_contains="healthy"), transport
    )
    assert result.success is False
    assert "healthy" in result.reason


async def test_body_keyword_present_is_success() -> None:
    transport: Transport = FakeTransport(
        responses={"https://example.com": (200, "status: healthy\nok")}
    )
    result = await run_http_check(
        http_config("https://example.com", body_contains="healthy"), transport
    )
    assert result.success is True


async def test_within_max_latency_is_success() -> None:
    # The over-ceiling case is exercised at the pure classifier layer
    # (test_results.py::test_max_latency_exceeded_failure) with a controlled
    # latency value. The executor measures real wall-clock time and pipes it
    # into the classifier, so an over-ceiling executor test would depend on an
    # uncontrollable clock — it belongs in the pure layer, not here.
    transport: Transport = FakeTransport(responses={"https://example.com": (200, "ok")})
    result = await run_http_check(
        http_config("https://example.com", max_latency_ms=10_000), transport
    )
    assert result.success is True


async def test_connection_refused_is_failure_with_reason() -> None:
    transport: Transport = FakeTransport(
        raise_on={"https://down.example.com": httpx.ConnectError("refused")}
    )
    result = await run_http_check(http_config("https://down.example.com"), transport)
    assert result.success is False
    assert "connection failed" in result.reason
    assert result.status_code is None
    assert result.latency_ms is not None


async def test_timeout_is_failure_with_reason() -> None:
    transport: Transport = FakeTransport(
        raise_on={"https://slow.example.com": httpx.TimeoutException("read")}
    )
    result = await run_http_check(http_config("https://slow.example.com", timeout_s=2.0), transport)
    assert result.success is False
    assert "timed out" in result.reason
    assert "2.0s" in result.reason


async def test_dispatcher_runs_http_kind() -> None:
    """run_check routes to the http executor by check_kind."""
    transport: Transport = FakeTransport(responses={"https://example.com": (200, "")})
    result = await run_check(http_config("https://example.com"), transport)
    assert result.success is True


async def test_dispatcher_rejects_unknown_kind_with_failing_result() -> None:
    """An unknown kind must not crash the engine — it yields a failing Result."""
    from tgmonitor.executors.base import CheckConfig

    transport: Transport = FakeTransport()
    result = await run_check(
        CheckConfig(monitor_id=1, check_kind="icmp", target="1.2.3.4"), transport
    )
    assert result.success is False
    assert "icmp" in result.reason


def test_fake_transport_satisfies_transport_protocol() -> None:
    """Structural check: the fake quacks like a Transport."""
    assert isinstance(FakeTransport(), Transport)


async def test_internal_target_refused() -> None:
    """An SSRF-internal HTTP target is refused before any request (#22)."""
    transport: Transport = FakeTransport(responses={"http://169.254.169.254/": (200, "x")})
    result = await run_http_check(http_config("http://169.254.169.254/"), transport)
    assert result.success is False
    assert "blocked" in result.reason
    # The transport must never have been called for a blocked target.
    assert transport.requested == []
