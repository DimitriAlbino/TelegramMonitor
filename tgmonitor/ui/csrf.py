"""CSRF protection for state-changing web UI requests (#30).

Enforced via an Origin header check: for unsafe methods (POST/PUT/PATCH/DELETE)
on the cookie-authenticated UI, the ``Origin`` (or ``Referer`` fallback) must
match the deployment's own origin. A cross-site forged request carries the
attacker's origin (or none), so it is rejected. This needs no client
cooperation and is the OWASP-recommended primary CSRF defense; on top of
SameSite=Lax it also covers same-site-subdomain attacks.

The check runs via :class:`OriginCsrfMiddleware`, mounted once on the app. It
exempts the JSON API (Bearer-authenticated, not vulnerable to CSRF) and the
Telegram webhook (validated by a separate secret header).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Path prefixes that use Bearer auth or their own secret validation, not the
# browser cookie — CSRF does not apply to them. The cookie-authenticated UI lives
# under /ui, so that is what we protect.
CSRF_PROTECTED_PREFIXES = ("/ui",)


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
        # None → derive per-request from settings (public host, plus localhost
        # only outside production, #43). An explicit set overrides (tests).
        self._permitted_hosts = permitted_hosts

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

            settings = get_settings()
            if self._permitted_hosts is not None:
                hosts = set(self._permitted_hosts)
            else:
                # Derived allow-list: the deployment's own host always; localhost
                # variants only outside production so a prod origin check cannot
                # be satisfied by a localhost Origin (#43).
                hosts = set()
                if settings.environment != "production":
                    hosts |= {"localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"}
            own = urlparse(settings.public_base_url)
            if own.netloc:
                hosts.add(own.netloc.lower())
            origin = request.headers.get("origin") or request.headers.get("referer")
            if not _origin_matches(origin, hosts):
                return Response("CSRF: invalid origin", status_code=403)
        return await call_next(request)
