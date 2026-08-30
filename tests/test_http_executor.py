"""Seam B tests — the HTTP Check executor with a fake transport.

These assert what the executor *does* given transport outcomes, never touching
the network. Cases mirror the launch spec's Seam B list:
  200 → success; 500 → failure; 200 without expected keyword → failure;
  200 over max-latency → failure; connection refused → failure with reason;
  timed out → failure with reason.
"""

from __future__ import annotations

from contextlib import contextmanager

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


# --- Pinned-candidate fallback (dual-stack reachability) ---
#
# The transport resolves a hostname to ordered candidates (IPv4 first — see
# pick_safe_ips) and tries each on ConnectError, so a dual-stack target stays
# monitorable from a single-stack network (e.g. a Docker bridge without IPv6).
# These tests drive HttpTransport with a MockTransport and a faked resolver:
# the handler sees the pinned IP as the URL host, so it can fail per-address.


@contextmanager
def _patched_resolver(addrs: list[str]):
    """Point the module resolver seam at a fixed answer for the block."""
    from tgmonitor.executors import ssrf

    orig = ssrf._default_resolver
    ssrf._default_resolver = lambda _h: addrs
    try:
        yield
    finally:
        ssrf._default_resolver = orig


async def test_pinned_candidates_tried_ipv4_first() -> None:
    """v6 listed first in DNS must not be attempted first: IPv4 wins ordering."""
    import httpx

    from tgmonitor.executors.http import HttpTransport, run_http_check

    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host or "")
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpTransport(client)
    with _patched_resolver(["2606:4700::1", "93.184.216.34"]):
        result = await run_http_check(http_config("https://example.test"), transport)
    await client.aclose()
    assert result.success is True
    assert seen == ["93.184.216.34"]  # IPv4 answered; v6 never needed


async def test_pinned_candidates_fall_back_on_connect_error() -> None:
    """A refused first candidate falls through to the next vetted address."""
    import httpx

    from tgmonitor.executors.http import HttpTransport, run_http_check

    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host or "")
        if request.url.host == "93.184.216.34":
            raise httpx.ConnectError("network is unreachable")
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpTransport(client)
    with _patched_resolver(["2606:4700::1", "93.184.216.34"]):
        result = await run_http_check(http_config("https://example.test"), transport)
    await client.aclose()
    assert result.success is True
    assert seen == ["93.184.216.34", "2606:4700::1"]


async def test_all_candidates_failing_reports_connect_error() -> None:
    """Exhausting every candidate surfaces the last connect error as failure."""
    import httpx

    from tgmonitor.executors.http import HttpTransport, run_http_check

    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host or "")
        raise httpx.ConnectError("network is unreachable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpTransport(client)
    with _patched_resolver(["93.184.216.34", "2606:4700::1"]):
        result = await run_http_check(http_config("https://example.test"), transport)
    await client.aclose()
    assert result.success is False
    assert "connection failed" in result.reason
    assert seen == ["93.184.216.34", "2606:4700::1"]


async def test_connect_timeout_does_not_fan_out() -> None:
    """A blackholed address must not multiply the probe clock: no per-IP retry."""
    import httpx

    from tgmonitor.executors.http import HttpTransport, run_http_check

    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host or "")
        raise httpx.ConnectTimeout("timed out")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpTransport(client)
    with _patched_resolver(["93.184.216.34", "2606:4700::1"]):
        result = await run_http_check(http_config("https://example.test"), transport)
    await client.aclose()
    assert result.success is False
    assert "timed out" in result.reason
    assert seen == ["93.184.216.34"]  # no fallback after a timeout
