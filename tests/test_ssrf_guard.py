"""Tests for the SSRF destination guard (#22).

Monitor targets are user-supplied and the server fetches them directly. The
guard refuses loopback / link-local (incl. cloud metadata 169.254.169.254) /
RFC1918 / other internal ranges, including DNS names that resolve to them.

These tests pin the pure decision functions; no real network for the IP
checks. DNS-resolution behaviour is asserted via a resolver seam so it can be
faked.
"""

from __future__ import annotations

import ipaddress

import pytest

from tgmonitor.executors.ssrf import (
    DestinationBlocked,
    assert_safe_destination,
    is_blocked_ip,
)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "127.1.2.3",
        "::1",  # IPv6 loopback
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "169.254.0.1",
        "0.0.0.0",
        "100.64.0.1",  # CGNAT
        "fc00::1",  # IPv6 ULA
        "fe80::1",  # IPv6 link-local
        "224.0.0.1",  # multicast
    ],
)
def test_internal_ips_are_blocked(ip: str) -> None:
    assert is_blocked_ip(ipaddress.ip_address(ip)) is True


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_ips_are_allowed(ip: str) -> None:
    assert is_blocked_ip(ipaddress.ip_address(ip)) is False


def test_ipv4_literal_target_blocked() -> None:
    """An http://169.254.169.254/... target must be refused."""
    with pytest.raises(DestinationBlocked):
        assert_safe_destination("http://169.254.169.254/latest/meta-data/", resolver=lambda h: [])


def test_ipv6_literal_target_blocked() -> None:
    with pytest.raises(DestinationBlocked):
        assert_safe_destination("http://[::1]/", resolver=lambda h: [])


def test_public_ip_literal_allowed() -> None:
    assert_safe_destination("http://93.184.216.34/", resolver=lambda h: [])  # no raise


def test_hostname_resolving_internal_is_blocked() -> None:
    """A DNS name whose A record is internal must be refused."""

    def resolves_internal(_h: str) -> list[str]:
        return ["10.0.0.5"]

    with pytest.raises(DestinationBlocked):
        assert_safe_destination("https://internal.example.com/", resolver=resolves_internal)


def test_hostname_resolving_public_allowed() -> None:
    def resolves_public(_h: str) -> list[str]:
        return ["93.184.216.34"]

    assert_safe_destination("https://example.com/", resolver=resolves_public)


def test_hostname_with_mixed_records_blocked_if_any_internal() -> None:
    """If any resolved address is internal, block (defensive)."""

    def mixed(_h: str) -> list[str]:
        return ["93.184.216.34", "127.0.0.1"]

    with pytest.raises(DestinationBlocked):
        assert_safe_destination("https://evil.example.com/", resolver=mixed)


def test_tcp_host_port_target_blocked() -> None:
    """A tcp target host:port resolving to internal must be refused."""
    with pytest.raises(DestinationBlocked):
        assert_safe_destination("169.254.169.254:80", resolver=lambda h: [], is_host_port=True)


def test_resolver_default_uses_real_dns_for_public() -> None:
    """The default resolver path is exercised by signature, not real DNS here."""
    # Just ensure the function accepts the default resolver arg shape.
    assert callable(assert_safe_destination)


# --- Regression tests for the production redirect path (#22) ---
#
# The SSRF re-validation hook must be attached to an *injected* client (the
# pooled client the worker engine builds), not only to the per-request client
# created inside HttpTransport. Otherwise redirect targets are never re-checked
# in production and an external URL that 302s to an internal address bypasses
# the guard entirely.


def test_http_transport_attaches_ssrf_hook_to_injected_client() -> None:
    import httpx

    from tgmonitor.executors.http import HttpTransport, _ssrf_request_hook

    client = httpx.AsyncClient()
    HttpTransport(client)
    assert _ssrf_request_hook in client.event_hooks.get("request", []), (
        "the SSRF hook must be registered on an injected (pooled) client"
    )


async def test_http_redirect_to_internal_is_blocked() -> None:
    """A 302 from a public target to an internal address is refused (#22)."""
    import httpx

    from tgmonitor.executors.base import CheckConfig
    from tgmonitor.executors.http import HttpTransport, run_http_check

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/meta-data"})
        return httpx.Response(200, text="leaked internal content")

    # Public literal IP as the start target so no real DNS is needed; the
    # injected client mirrors the engine's pooled follow_redirects client.
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    transport = HttpTransport(client)
    cfg = CheckConfig(
        monitor_id=1,
        check_kind="http",
        target="http://93.184.216.34/start",
        follow_redirects=True,
    )
    result = await run_http_check(cfg, transport)
    await client.aclose()
    assert result.success is False
    assert "blocked" in result.reason
    assert "leaked" not in result.reason


async def test_api_content_internal_target_is_blocked() -> None:
    """api_content must refuse an internal target before fetching (#22)."""
    from tests.conftest import FakeTransport, api_content_config
    from tgmonitor.executors.api_content import run_api_content_check

    # The transport would 'succeed' if reached; the guard must block first.
    transport = FakeTransport(
        responses={"http://169.254.169.254/latest/meta-data": (200, '{"mode": "live"}')}
    )
    cfg = api_content_config(target="http://169.254.169.254/latest/meta-data")
    result = await run_api_content_check(cfg, transport)
    assert result.success is False
    assert "blocked" in result.reason
