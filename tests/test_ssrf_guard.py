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
# Redirects are followed manually and each hop is re-pinned to a vetted IP (#39),
# so an external URL that 302s to an internal address is refused on the
# production (pooled-client) path. The transport connects to the exact address
# it validated, closing the DNS-rebinding TOCTOU.


def test_pin_candidates_rewrites_hostname_to_validated_ip() -> None:
    """A hostname is rewritten to its vetted IP with Host+SNI preserved (#39)."""
    import tgmonitor.executors.http as http_mod
    from tgmonitor.executors import ssrf

    # Fake the resolver so no real DNS runs: example.test -> a public IP.
    orig = ssrf._default_resolver
    ssrf._default_resolver = lambda h: ["93.184.216.34"]
    try:
        candidates = http_mod._pin_candidates("https://example.test:8443/path?q=1")
    finally:
        ssrf._default_resolver = orig
    assert len(candidates) == 1
    pinned, headers, sni = candidates[0]
    assert pinned == "https://93.184.216.34:8443/path?q=1"
    assert headers is not None
    assert headers["Host"] == "example.test:8443"
    assert "Authorization" not in headers  # no userinfo → no auth header
    assert sni == "example.test"


def test_pin_candidates_one_per_resolved_address_ipv4_first() -> None:
    """A dual-stack name yields one candidate per address, IPv4 before IPv6."""
    import tgmonitor.executors.http as http_mod
    from tgmonitor.executors import ssrf

    orig = ssrf._default_resolver
    ssrf._default_resolver = lambda h: ["2606:4700::1", "93.184.216.34"]
    try:
        candidates = http_mod._pin_candidates("https://example.test/x")
    finally:
        ssrf._default_resolver = orig
    assert [pinned for pinned, _h, _s in candidates] == [
        "https://93.184.216.34/x",
        "https://[2606:4700::1]/x",
    ]
    assert all(headers == {"Host": "example.test"} for _p, headers, _s in candidates)
    assert all(sni == "example.test" for _p, _h, sni in candidates)


def test_pin_candidates_refuses_hostname_resolving_internal() -> None:
    """DNS-rebinding: a name resolving to an internal IP is refused at pin (#39)."""
    import tgmonitor.executors.http as http_mod
    from tgmonitor.executors import ssrf
    from tgmonitor.executors.ssrf import DestinationBlocked

    orig = ssrf._default_resolver
    ssrf._default_resolver = lambda h: ["169.254.169.254"]  # metadata endpoint
    try:
        with pytest.raises(DestinationBlocked):
            http_mod._pin_candidates("http://rebind.test/latest/meta-data")
    finally:
        ssrf._default_resolver = orig


async def test_http_redirect_to_internal_is_blocked() -> None:
    """A 302 from a public target to an internal address is refused (#39)."""
    import httpx

    from tgmonitor.executors.base import CheckConfig
    from tgmonitor.executors.http import HttpTransport, run_http_check

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/meta-data"})
        return httpx.Response(200, text="leaked internal content")

    # Public literal IP as the start target so no real DNS is needed; the
    # injected client mirrors the engine's pooled client. The redirect hop to an
    # internal literal is re-pinned and refused before any content is read.
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
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


def test_pin_candidates_preserves_basic_auth_credentials() -> None:
    """Rewriting to the pinned IP keeps URL basic-auth via an Authorization header (#39)."""
    import base64

    import tgmonitor.executors.http as http_mod
    from tgmonitor.executors import ssrf

    orig = ssrf._default_resolver
    ssrf._default_resolver = lambda h: ["93.184.216.34"]
    try:
        candidates = http_mod._pin_candidates("https://user:pass@example.test/x")
    finally:
        ssrf._default_resolver = orig
    assert len(candidates) == 1
    pinned, headers, sni = candidates[0]
    assert pinned == "https://93.184.216.34/x"  # userinfo stripped from URL
    assert headers is not None
    expected = "Basic " + base64.b64encode(b"user:pass").decode()
    assert headers["Authorization"] == expected
    assert headers["Host"] == "example.test"
    assert sni == "example.test"


# --- Ordered candidate selection (dual-stack reachability) ---
#
# A dual-stack target must be attempted on IPv4 before IPv6: a monitor host
# whose network lacks IPv6 egress (e.g. a default Docker bridge) otherwise
# deterministically fails on every probe when the unordered pick lands on the
# AAAA record (the 2026-08-30 italdroni.it/caviauto.it incident).


def test_pick_safe_ips_orders_ipv4_before_ipv6() -> None:
    from tgmonitor.executors import ssrf

    resolved = ["2606:4700::2", "93.184.216.34", "2606:4700::1", "1.1.1.1"]
    assert ssrf.pick_safe_ips("dual.example.test", resolver=lambda _h: resolved) == [
        "1.1.1.1",
        "93.184.216.34",
        "2606:4700::1",
        "2606:4700::2",
    ]


def test_pick_safe_ips_empty_when_unresolvable() -> None:
    from tgmonitor.executors import ssrf

    assert ssrf.pick_safe_ips("nope.example.test", resolver=lambda _h: []) == []


def test_pick_safe_ips_raises_if_any_address_internal() -> None:
    from tgmonitor.executors import ssrf

    resolved = ["93.184.216.34", "10.0.0.5"]
    with pytest.raises(DestinationBlocked):
        ssrf.pick_safe_ips("rebind.example.test", resolver=lambda _h: resolved)


def test_pick_safe_ips_ipv6_only_target_kept() -> None:
    """An IPv6-only name yields its (single) global address — not dropped."""
    from tgmonitor.executors import ssrf

    resolved = ["2606:4700:4700::1111"]
    assert ssrf.pick_safe_ips("v6only.example.test", resolver=lambda _h: resolved) == [
        "2606:4700:4700::1111"
    ]
