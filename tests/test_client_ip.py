"""Tests for proxy-aware client IP extraction and auth abuse resistance (#29).

Behind Caddy, ``request.client.host`` is the proxy IP for everyone, collapsing
all users into one rate-limit bucket and letting the shared cap lock out the
whole user base. The client IP must be derived from the trusted
X-Forwarded-For header, and not be spoofable from arbitrary clients.
"""

from __future__ import annotations

from tgmonitor.auth.ratelimit import client_ip_from_request


class _FakeClient:
    def __init__(self, host: str | None) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, client_host: str | None, headers: dict[str, str]) -> None:
        self.client = _FakeClient(client_host)
        self.headers = headers


def test_uses_rightmost_forwarded_for_when_trusted() -> None:
    """Trust the right-most hop — the peer address Caddy appended (#29).

    Caddy appends the real peer to any client-supplied X-Forwarded-For, so the
    right-most entry is what our proxy observed; left-most entries are
    client-controlled and must be ignored.
    """
    req = _FakeRequest("127.0.0.1", {"x-forwarded-for": "10.0.0.1, 203.0.113.9"})
    assert client_ip_from_request(req, trust_proxy=True) == "203.0.113.9"


def test_spoofed_forwarded_for_prefix_is_not_trusted() -> None:
    """A client that injects its own X-Forwarded-For cannot control the result.

    The client sends a bogus left-most value; Caddy appends the real peer. We
    must return the appended peer, never the spoofed prefix — otherwise the
    per-IP limiter is trivially evaded by rotating the header.
    """
    req = _FakeRequest("127.0.0.1", {"x-forwarded-for": "1.2.3.4, 198.51.100.7"})
    assert client_ip_from_request(req, trust_proxy=True) == "198.51.100.7"


def test_ignores_x_forwarded_for_when_not_trusted() -> None:
    """Without trust_proxy, the header is ignored (not spoofable)."""
    req = _FakeRequest("127.0.0.1", {"x-forwarded-for": "203.0.113.9"})
    assert client_ip_from_request(req, trust_proxy=False) == "127.0.0.1"


def test_falls_back_to_client_host_without_header() -> None:
    req = _FakeRequest("198.51.100.7", {})
    assert client_ip_from_request(req, trust_proxy=True) == "198.51.100.7"


def test_handles_missing_client() -> None:
    req = _FakeRequest(None, {"x-forwarded-for": "203.0.113.9"})
    assert client_ip_from_request(req, trust_proxy=True) == "203.0.113.9"


def test_strips_whitespace_in_forwarded_for() -> None:
    req = _FakeRequest("127.0.0.1", {"x-forwarded-for": " 10.0.0.1 , 203.0.113.9 "})
    assert client_ip_from_request(req, trust_proxy=True) == "203.0.113.9"
