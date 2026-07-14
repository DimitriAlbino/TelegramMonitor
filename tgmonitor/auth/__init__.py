"""Authentication: password hashing, JWT sessions, rate limiting, and the
FastAPI dependency that resolves the current User from a Bearer token.
"""

from __future__ import annotations

from .dependencies import CurrentUser, get_current_user, require_user
from .tokens import (
    create_purpose_token,
    create_session_token,
    decode_token,
    hash_password,
    verify_password,
)

__all__ = [
    "CurrentUser",
    "create_purpose_token",
    "create_session_token",
    "decode_token",
    "get_current_user",
    "hash_password",
    "require_user",
    "verify_password",
]
