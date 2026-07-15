"""Tests for proxy-aware client IP extraction and auth abuse resistance (#29).

Behind Caddy, ``request.client.host`` is the proxy IP for everyone, collapsing
all users into one rate-limit bucket and letting the shared cap lock out the
whole user base. The client IP must be derived from the trusted
X-Forwarded-For header, and not be spoofable from arbitrary clients.
"""

from __future__ import annotations

import pytest

from tgmonitor.auth.ratelimit import client_ip_from_request


class _FakeClient:
    def __init__(self, host: str | None) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, client_host: str | None, headers: dict[str, str]) -> None:
        self.client = _FakeClient(client_host)
        self.headers = headers


def test_uses_x_forwarded_for_when_trusted() -> None:
    req = _FakeRequest("127.0.0.1", {"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    assert client_ip_from_request(req, trust_proxy=True) == "203.0.113.9"


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
    req = _FakeRequest("127.0.0.1", {"x-forwarded-for": " 203.0.113.9 , 10.0.0.1"})
    assert client_ip_from_request(req, trust_proxy=True) == "203.0.113.9"
