"""FastAPI app: healthz, auth, and recent-Results endpoint.

Monitor reads are ownership-scoped: an authenticated User only sees Monitors
they own (ADR-0001 tenant isolation). Vocabulary is normative: the response
field is ``results``, each item a Check Result (CONTEXT.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.api.monitors import router as monitors_router
from tgmonitor.api.reports import router as reports_router
from tgmonitor.api.statuspage import config_router as statuspage_config_router
from tgmonitor.api.statuspage import public_router as statuspage_public_router
from tgmonitor.auth.dependencies import ActiveUser
from tgmonitor.auth.routes import router as auth_router
from tgmonitor.auth.tokens import decode_token
from tgmonitor.db import dispose_engine, get_engine, get_session
from tgmonitor.models import Check, Monitor, User
from tgmonitor.telegram.webhook import router as telegram_router
from tgmonitor.ui.app_routes import router as ui_app_router
from tgmonitor.ui.auth_routes import router as ui_auth_router
from tgmonitor.ui.session import COOKIE_NAME

# Annotated dependency aliases — the idiomatic FastAPI form that also satisfies
# the "no function call in default" lint rule (B008).
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _result_to_dict(check: Check) -> dict[str, object]:
    return {
        "monitor_id": check.monitor_id,
        "checked_at": check.checked_at.isoformat() if check.checked_at else None,
        "success": check.success,
        "status_code": check.status_code,
        "latency_ms": check.latency_ms,
        "reason": check.reason,
    }


async def _get_owned_monitor(monitor_id: int, user: User, session: AsyncSession) -> Monitor:
    """Fetch a Monitor and assert it belongs to ``user``; 404 otherwise.

    Returning 404 (not 403) for another tenant's monitor avoids leaking which
    monitor ids exist — the tenant boundary is invisible to the caller.
    """
    monitor = await session.get(Monitor, monitor_id)
    if monitor is None or monitor.user_id != user.id:
        raise HTTPException(status_code=404, detail="monitor not found")
    return monitor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="TelegramMonitor",
        description="Hosted monitoring service with Telegram alerts.",
        version="0.1.0",
        lifespan=lifespan,
    )
    # CSRF: reject cross-origin unsafe requests to the cookie-authenticated UI
    # (#30). The Bearer-authenticated JSON API and the Telegram webhook (secret
    # header) are exempt.
    from tgmonitor.ui.csrf import OriginCsrfMiddleware

    app.add_middleware(OriginCsrfMiddleware)
    app.include_router(auth_router)
    app.include_router(monitors_router)
    app.include_router(reports_router)
    app.include_router(statuspage_config_router)
    app.include_router(statuspage_public_router)
    app.include_router(telegram_router)
    app.include_router(ui_auth_router)
    app.include_router(ui_app_router)
    # Serve static assets (CSS) and mount the templates directory.
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # Root redirect: logged-out → login, logged-in → dashboard. Fixes the bare
    # 404 at "/" — the entry point for browser users.
    @app.get("/", include_in_schema=False)
    async def root_redirect(request: Request) -> RedirectResponse:
        token = request.cookies.get(COOKIE_NAME)
        if token and decode_token(token, expected_purpose="session"):
            return RedirectResponse("/ui/monitors", status_code=302)
        return RedirectResponse("/ui/login", status_code=302)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, object]:
        """Liveness probe for an external uptime monitor.

        Separates DB reachability (liveness) from probe volume (freshness) per
        the reference pattern. A DB error makes us report ``degraded`` rather
        than 500 — an external monitor can distinguish "process up, DB down"
        from "process down".
        """
        status = "ok"
        db_ok = True
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            db_ok = False
            status = "degraded"
        return {"status": status, "db": db_ok}

    @app.get("/monitors/{monitor_id}/results", tags=["monitors"])
    async def list_monitor_results(
        monitor_id: int,
        user: ActiveUser,
        session: SessionDep,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, object]:
        """Return the most recent Check Results for a Monitor (newest first).

        Ownership-scoped: a User only sees their own Monitors.
        """
        monitor = await _get_owned_monitor(monitor_id, user, session)

        stmt = (
            select(Check)
            .where(Check.monitor_id == monitor_id)
            .order_by(desc(Check.checked_at))
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {
            "monitor_id": monitor_id,
            "name": monitor.name,
            "results": [_result_to_dict(c) for c in rows],
        }

    return app


app = create_app()
