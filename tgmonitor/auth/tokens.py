"""Password hashing and signed session/token utilities.

Tokens (sessions, verification, reset) are all JWTs signed with ``SECRET_KEY``.
Each purpose-scoped token carries a ``jti`` (JWT id) so single-use semantics
can be enforced: the matching ``*_token_jti`` on the User row must equal the
token's jti, and is cleared on consumption.

- session tokens: carry ``user_id``, expire after ``session_expire_minutes``.
- verify/reset tokens: carry ``user_id`` + ``purpose``, single-use.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from passlib.context import CryptContext

from tgmonitor.config import get_settings

# argon2 is the default; bcrypt is accepted for legacy hashes if ever needed.
_pwd = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")

ALGORITHM = "HS256"


def hash_password(plaintext: str) -> str:
    return str(_pwd.hash(plaintext))


def verify_password(plaintext: str, hashed: str) -> bool:
    try:
        return bool(_pwd.verify(plaintext, hashed))
    except (ValueError, TypeError):
        return False


def _new_jti() -> str:
    return secrets.token_urlsafe(24)


@dataclass(frozen=True, slots=True)
class TokenPayload:
    sub: str  # user id as str
    purpose: str
    jti: str
    exp: datetime


def _encode(payload: dict[str, object]) -> str:
    settings = get_settings()
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def create_session_token(user_id: int) -> str:
    settings = get_settings()
    exp = datetime.now(UTC) + timedelta(minutes=settings.session_expire_minutes)
    payload = {
        "sub": str(user_id),
        "purpose": "session",
        "jti": _new_jti(),
        "exp": exp,
    }
    return _encode(payload)


def create_purpose_token(user_id: int, purpose: str, ttl_minutes: int = 60) -> tuple[str, str]:
    """Create a one-time purpose token (verify/reset). Returns (token, jti)."""
    exp = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
    jti = _new_jti()
    payload = {"sub": str(user_id), "purpose": purpose, "jti": jti, "exp": exp}
    return _encode(payload), jti


def decode_token(token: str, expected_purpose: str | None = None) -> TokenPayload | None:
    """Decode and validate a token. Returns None on any failure.

    Never raises — callers treat a bad/expired/wrong-purpose token as invalid.
    """
    settings = get_settings()
    try:
        raw = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    purpose = str(raw.get("purpose", ""))
    if expected_purpose is not None and purpose != expected_purpose:
        return None
    try:
        exp = datetime.fromtimestamp(float(raw["exp"]), tz=UTC)
    except (KeyError, ValueError, OSError):
        return None
    return TokenPayload(sub=str(raw["sub"]), purpose=purpose, jti=str(raw["jti"]), exp=exp)
