"""CSRF protection for state-changing web UI requests (#30).

Two complementary defenses (defense-in-depth), neither requiring template edits:

1. **Origin check** (primary): for unsafe methods (POST/PUT/PATCH/DELETE) on the
   cookie-authenticated UI, the ``Origin`` (or ``Referer`` fallback) must match
   the deployment's own origin. A cross-site forged request carries the
   attacker's origin (or none), so it is rejected. This needs no client
   cooperation and is the OWASP-recommended primary CSRF defense.

2. **Double-submit token** (secondary, available): ``generate_csrf_token`` +
   ``set_csrf_cookie`` issue a ``tgm_csrf`` cookie that forms can post back as
   ``csrf_token``; ``validate_csrf`` compares them. SameSite=Lax already blocks
   the classic cross-site form POST; this adds same-site-subdomain coverage.

The Origin check is enforced via :func:`OriginCsrfMiddleware`, mounted once on
the app. It exempts the JSON API (Bearer-authenticated, not vulnerable to CSRF)
and the Telegram webhook (validated by a separate secret header).
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Cookie, Depends, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

CSRF_COOKIE_NAME = "tgm_csrf"
CSRF_FIELD = "csrf_token"

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Path prefixes that use Bearer auth or their own secret validation, not the
# browser cookie — CSRF does not apply to them. The cookie-authenticated UI lives
# under /ui, so that is what we protect.
CSRF_PROTECTED_PREFIXES = ("/ui",)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str | None = None, *, secure: bool = False) -> str:
    """Set the CSRF cookie on a response and return the token value."""
    value = token or generate_csrf_token()
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=value,
        httponly=False,
        samesite="lax",
        secure=secure,
        max_age=60 * 60 * 24 * 14,
    )
    return value


def _origin_matches(request_origin: str | None, own_hosts: set[str]) -> bool:
    if not request_origin:
        return False
    host = urlparse(request_origin).netloc.lower()
    return host in own_hosts


class OriginCsrfMiddleware(BaseHTTPMiddleware):
    """Reject cross-origin unsafe requests to the cookie-authenticated UI (#30).

    For unsafe methods on CSRF-protected prefixes, the request's ``Origin`` (or
    ``Referer`` netloc) must match one of the permitted hosts (the deployment's
    own public host, plus localhost for dev). GETs and the Bearer-authenticated
    JSON API are exempt.
    """

    def __init__(self, app: object, *, permitted_hosts: set[str] | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._permitted_hosts = permitted_hosts or {"localhost", "127.0.0.1"}

    def _is_protected(self, request: StarletteRequest) -> bool:
        if request.method not in UNSAFE_METHODS:
            return False
        return any(request.url.path.startswith(p) for p in CSRF_PROTECTED_PREFIXES)

    async def dispatch(
        self,
        request: StarletteRequest,
        call_next: Callable[[StarletteRequest], Awaitable[Response]],
    ) -> Response:
        if self._is_protected(request):
            # Lazy-import settings to avoid circular import at module load.
            from tgmonitor.config import get_settings

            hosts = set(self._permitted_hosts)
            own = urlparse(get_settings().public_base_url)
            if own.netloc:
                hosts.add(own.netloc.lower())
            origin = request.headers.get("origin") or request.headers.get("referer")
            if not _origin_matches(origin, hosts):
                return Response("CSRF: invalid origin", status_code=status.HTTP_403_FORBIDDEN)
        return await call_next(request)


async def validate_csrf(
    request: Request,
    tgm_csrf: Annotated[str | None, Cookie()] = None,
) -> None:
    """Optional double-submit token dependency for routes that opt in.

    Compares the posted ``csrf_token`` form field to the ``tgm_csrf`` cookie in
    constant time. The Origin middleware is the primary defense; this is
    available for forms that additionally post a token.
    """
    form = await request.form()
    posted = form.get(CSRF_FIELD)
    if not posted or not tgm_csrf or not secrets.compare_digest(str(posted), tgm_csrf):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing or invalid"
        )


CSRFDep = Annotated[None, Depends(validate_csrf)]
