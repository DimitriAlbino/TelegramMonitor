"""FastAPI dependencies for authentication.

- :func:`get_current_user` resolves the Bearer token to a User (or 401).
- :func:`require_user` additionally requires the account to be active/verified.

Both yield the User so routes can scope reads/writes by ``user.id`` — this is
the tenant isolation boundary (ADR-0001): a User only ever sees their own rows.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.db import get_session
from tgmonitor.models import User

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    """Resolve the Bearer token to a User. Raises 401 on any failure."""
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    from tgmonitor.auth.tokens import decode_token

    payload = decode_token(creds.credentials, expected_purpose="session")
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired token"
        )
    try:
        user_id = int(payload.sub)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token subject"
        ) from exc
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    # Session revocation (#30): reject tokens issued before the user's
    # session_version was bumped (logout / password change / reset).
    if payload.sv != user.session_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session revoked")
    return user


async def require_user(user: Annotated[User, Depends(get_current_user)]) -> User:
    """Require an active (verified) account. Use on routes that act."""
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account not verified")
    return user


# Convenient annotated aliases for route signatures.
CurrentUser = Annotated[User, Depends(get_current_user)]
ActiveUser = Annotated[User, Depends(require_user)]
