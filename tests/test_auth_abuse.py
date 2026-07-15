"""Tests for auth abuse-resistance (#29).

Covers the constant-time login path (a missing User still runs a real argon2
verify against a dummy hash so response timing does not branch on user
existence) and the reset limiter enforcement (the result was previously
discarded). These exercise the helpers + token logic without a running server.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

import tgmonitor.auth.routes as auth_routes
from tgmonitor.auth import tokens


def test_dummy_hash_is_a_valid_argon2_hash() -> None:
    """The dummy hash used for constant-time login must be a real argon2 hash."""
    assert auth_routes._DUMMY_HASH.startswith("$argon2id$")
    # It must verify against its own plaintext (proving it's a well-formed hash
    # that costs the same to check as a real one).
    assert tokens.verify_password("constant-time-dummy-do-not-use", auth_routes._DUMMY_HASH)


def test_dummy_hash_rejects_other_passwords() -> None:
    assert tokens.verify_password("anything-else", auth_routes._DUMMY_HASH) is False


async def test_login_constant_time_runs_verify_for_missing_user(monkeypatch) -> None:
    """A login for an unregistered email must still run verify_password (#29).

    We patch verify_password to record the hash it received and assert the dummy
    hash is checked (not short-circuited) when no User row exists.
    """
    seen_hashes: list[str] = []
    real_verify = tokens.verify_password

    def recording_verify(plaintext, hashed):
        seen_hashes.append(hashed)
        return real_verify(plaintext, hashed)

    monkeypatch.setattr(auth_routes, "verify_password", recording_verify)
    monkeypatch.setattr(tokens, "verify_password", recording_verify)
    # Bypass the shared rate limiters so we reach the password check.
    from tgmonitor.auth import ratelimit as rl

    class _AlwaysAllow:
        async def check(self, _key):
            return True

    monkeypatch.setattr(rl, "get_ip_limiter", lambda: _AlwaysAllow())
    monkeypatch.setattr(rl, "get_email_limiter", lambda: _AlwaysAllow())
    monkeypatch.setattr(auth_routes, "get_ip_limiter", lambda: _AlwaysAllow())
    monkeypatch.setattr(auth_routes, "get_email_limiter", lambda: _AlwaysAllow())

    from fastapi import HTTPException

    class _ScalarSession:
        async def scalar(self, _stmt):
            return None

        async def commit(self):
            pass

    body = auth_routes.LoginIn(email="ghost@example.com", password="whatever")
    with pytest.raises(HTTPException) as exc:
        await auth_routes.login(
            body,
            request=_DummyRequest(),
            session=_ScalarSession(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 401
    assert seen_hashes, "verify_password must run even for a missing user"
    assert seen_hashes[0] == auth_routes._DUMMY_HASH


class _DummyClient:
    host = "127.0.0.1"


class _DummyRequest:
    client = _DummyClient()
    headers: ClassVar[dict[str, str]] = {}


async def test_reset_request_over_limit_raises_429(monkeypatch) -> None:
    """When the IP limiter rejects, request_reset raises 429 (enforced, #29).

    Behavioral: drive the real handler with a limiter that returns False and
    assert it raises HTTP 429 and never reaches the DB — proving the limiter
    result is honoured, not discarded.
    """
    from fastapi import HTTPException

    from tgmonitor.auth.routes import ResetRequestIn

    class _OverLimit:
        async def check(self, key):
            return False

    reached_db = False

    class _Session:
        async def scalar(self, stmt):
            nonlocal reached_db
            reached_db = True
            return None

    monkeypatch.setattr(auth_routes, "get_ip_limiter", lambda: _OverLimit())

    with pytest.raises(HTTPException) as ei:
        await auth_routes.request_reset(
            ResetRequestIn(email="user@example.com"), _DummyRequest(), _Session()
        )
    assert ei.value.status_code == 429
    assert reached_db is False, "must short-circuit before any DB work when throttled"
