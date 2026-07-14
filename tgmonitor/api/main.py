"""FastAPI app: healthz + recent-Results endpoint.

T1's GET endpoint is deliberately unauthenticated (auth lands in T2 and will
retire the seed). Vocabulary is normative: the response field is ``results``,
each item a Check Result (CONTEXT.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.db import dispose_engine, get_engine, get_session
from tgmonitor.models import Check, Monitor

# Annotated dependency alias — the idiomatic FastAPI form that also satisfies
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
        session: SessionDep,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, object]:
        """Return the most recent Check Results for a Monitor (newest first).

        T1: unauthenticated. T2 adds ownership scoping behind auth.
        """
        monitor = await session.get(Monitor, monitor_id)
        if monitor is None:
            raise HTTPException(status_code=404, detail="monitor not found")

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
