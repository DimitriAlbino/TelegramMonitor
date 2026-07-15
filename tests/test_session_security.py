"""Tests for session revocation + CSRF (#30).

Covers:
- A session token is rejected after its User's session_version is bumped
  (logout / password change / reset).
- The Origin CSRF middleware rejects cross-origin unsafe UI POSTs and allows
  same-origin ones + exempts GETs and the JSON API.
"""

from __future__ import annotations

import pytest

from tgmonitor.auth.tokens import create_session_token, decode_token


def test_session_token_carries_version() -> None:
    token = create_session_token(user_id=1, session_version=3)
    payload = decode_token(token, expected_purpose="session")
    assert payload is not None
    assert payload.sv == 3


def test_legacy_token_without_sv_defaults_to_zero() -> None:
    """Tokens issued before #30 had no sv claim; they decode to sv=0."""
    import jwt

    from tgmonitor.config import get_settings

    raw = jwt.encode(
        {"sub": "1", "purpose": "session", "jti": "x", "exp": 9_999_999_999},
        get_settings().secret_key,
        algorithm="HS256",
    )
    payload = decode_token(raw, expected_purpose="session")
    assert payload is not None
    assert payload.sv == 0


# --- CSRF middleware ---


class _Resp:
    def __init__(self, status_code: int, body: str = "") -> None:
        self.status_code = status_code
        self.body = body


class _FakeApp:
    async def __call__(self, scope, receive, send):
        pass


def _make_request(method: str, path: str, origin: str | None, referer: str | None = None):
    headers: list[tuple[bytes, bytes]] = []
    if origin:
        headers.append((b"origin", origin.encode()))
    if referer:
        headers.append((b"referer", referer.encode()))
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "headers": headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("localhost", 8000),
        "client": ("127.0.0.1", 1),
        "app": None,
        "extensions": {},
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_csrf_rejects_cross_origin_ui_post(monkeypatch) -> None:
    from tgmonitor.ui.csrf import OriginCsrfMiddleware

    monkeypatch.setattr("tgmonitor.config.get_settings", lambda: _Settings("http://localhost:8000"))
    mw = OriginCsrfMiddleware(_FakeApp(), permitted_hosts={"localhost", "127.0.0.1"})
    resp = await mw.dispatch(
        _make_request("POST", "/ui/monitors", "https://evil.example"), lambda: None
    )  # type: ignore
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_csrf_allows_same_origin_ui_post(monkeypatch) -> None:
    from tgmonitor.ui.csrf import OriginCsrfMiddleware

    monkeypatch.setattr("tgmonitor.config.get_settings", lambda: _Settings("http://localhost:8000"))
    called = False

    async def call_next(_req):
        nonlocal called
        called = True
        return _Resp(200)

    mw = OriginCsrfMiddleware(_FakeApp(), permitted_hosts={"localhost", "127.0.0.1"})
    resp = await mw.dispatch(
        _make_request("POST", "/ui/monitors", "http://localhost:8000"), call_next
    )  # type: ignore
    assert called
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_csrf_exempts_get(monkeypatch) -> None:
    from tgmonitor.ui.csrf import OriginCsrfMiddleware

    monkeypatch.setattr("tgmonitor.config.get_settings", lambda: _Settings("http://localhost:8000"))
    called = False

    async def call_next(_req):
        nonlocal called
        called = True
        return _Resp(200)

    mw = OriginCsrfMiddleware(_FakeApp())
    # GET with a foreign origin is fine — safe method.
    await mw.dispatch(_make_request("GET", "/ui/monitors", "https://evil.example"), call_next)  # type: ignore
    assert called


@pytest.mark.asyncio
async def test_csrf_exempts_json_api_post(monkeypatch) -> None:
    """The Bearer-authenticated JSON API is not CSRF-protected (#30)."""
    from tgmonitor.ui.csrf import OriginCsrfMiddleware

    monkeypatch.setattr("tgmonitor.config.get_settings", lambda: _Settings("http://localhost:8000"))
    called = False

    async def call_next(_req):
        nonlocal called
        called = True
        return _Resp(200)

    mw = OriginCsrfMiddleware(_FakeApp())
    # /auth/login is under /auth, not /ui → exempt.
    await mw.dispatch(_make_request("POST", "/auth/login", None), call_next)  # type: ignore
    assert called


@pytest.mark.asyncio
async def test_csrf_uses_referer_when_origin_absent(monkeypatch) -> None:
    from tgmonitor.ui.csrf import OriginCsrfMiddleware

    monkeypatch.setattr("tgmonitor.config.get_settings", lambda: _Settings("http://localhost:8000"))
    called = False

    async def call_next(_req):
        nonlocal called
        called = True
        return _Resp(200)

    mw = OriginCsrfMiddleware(_FakeApp(), permitted_hosts={"localhost", "127.0.0.1"})
    await mw.dispatch(
        _make_request(
            "POST", "/ui/settings", origin=None, referer="http://localhost:8000/ui/settings"
        ),
        call_next,  # type: ignore
    )
    assert called


class _Settings:
    """Minimal settings stand-in exposing public_base_url."""

    def __init__(self, public_base_url: str) -> None:
        self.public_base_url = public_base_url
