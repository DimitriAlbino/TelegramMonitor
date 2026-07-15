"""Cookie-based session dependency for HTML pages.

The JSON API authenticates via the ``Authorization: Bearer`` header; the web UI
needs cookie-based auth so HTML pages work in a browser. This dependency reads
the session token from a cookie (``tgm_session``) and resolves it to a User.

Both the header-based (``CurrentUser``) and cookie-based (``CurrentUserCookie``)
dependencies produce the same User — the underlying token is identical.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.auth.tokens import decode_token
from tgmonitor.db import get_session
from tgmonitor.models import User

COOKIE_NAME = "tgm_session"


async def get_current_user_cookie(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    tgm_session: Annotated[str | None, Cookie()] = None,
) -> User:
    """Resolve the session cookie to a User, or 401.

    Stores the resolved user on ``request.state.user`` so template renderers can
    show the nav without the route passing it explicitly.
    """
    if tgm_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"Location": "/ui/login"},
        )
    payload = decode_token(tgm_session, expected_purpose="session")
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired session"
        )
    user = await session.get(User, int(payload.sub))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    request.state.user = user
    return user


async def require_user_cookie(user: Annotated[User, Depends(get_current_user_cookie)]) -> User:
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account not verified")
    return user


CurrentUserCookie = Annotated[User, Depends(get_current_user_cookie)]
ActiveUserCookie = Annotated[User, Depends(require_user_cookie)]
