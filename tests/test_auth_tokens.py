"""Tests for the pure auth primitives: password hashing and JWT tokens.

These cover the cryptographic seams without touching the DB or FastAPI.
"""

from __future__ import annotations

from tgmonitor.auth.tokens import (
    create_purpose_token,
    create_session_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    h = hash_password("s3cret-pass")
    assert h != "s3cret-pass"
    assert verify_password("s3cret-pass", h) is True
    assert verify_password("wrong", h) is False


def test_password_hash_is_salt_random() -> None:
    """Same password produces different hashes (salt is per-hash)."""
    assert hash_password("same-pass") != hash_password("same-pass")


def test_session_token_roundtrip() -> None:
    token = create_session_token(user_id=42)
    payload = decode_token(token, expected_purpose="session")
    assert payload is not None
    assert payload.sub == "42"
    assert payload.purpose == "session"


def test_purpose_token_roundtrip() -> None:
    token, jti = create_purpose_token(user_id=7, purpose="verify")
    assert jti
    payload = decode_token(token, expected_purpose="verify")
    assert payload is not None
    assert payload.sub == "7"
    assert payload.purpose == "verify"
    assert payload.jti == jti


def test_wrong_purpose_rejected() -> None:
    token, _ = create_purpose_token(user_id=1, purpose="verify")
    assert decode_token(token, expected_purpose="reset") is None


def test_garbage_token_returns_none() -> None:
    assert decode_token("not.a.jwt", expected_purpose="session") is None


def test_tampered_token_rejected() -> None:
    token = create_session_token(user_id=1)
    # Flip the last character to tamper the signature.
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert decode_token(tampered, expected_purpose="session") is None
