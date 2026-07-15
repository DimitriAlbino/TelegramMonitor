"""Public Status Page routes (ADR-0009).

Two surfaces:
- ``GET /status/{slug}`` — the public, unauthenticated, cached page. Returns
  JSON (the web UI renders it) reflecting the live state of opted-in Monitors
  + Incident history. Cached ~60s so a traffic spike can't hammer the DB.
- ``/api/status-page`` (authenticated) — configure the user's page: create,
  update title/subtitle, set slug.

CONTEXT.md: the page is auto-generated from Check/Incident data; users do not
publish manual posts.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.auth.dependencies import ActiveUser
from tgmonitor.db import get_session
from tgmonitor.incident_model import Incident
from tgmonitor.models import Check, Monitor
from tgmonitor.statuspage_model import StatusPage

# Public (unauthenticated) + configured (authenticated) routers split so the
# OpenAPI docs and any future auth middleware can treat them differently.
public_router = APIRouter(prefix="/status", tags=["status-page"])
config_router = APIRouter(prefix="/api/status-page", tags=["status-page"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Cache the public page for this long (ADR-0009: ~60s).
CACHE_TTL_S = 60


class StatusPageCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(default="Status", max_length=255)
    subtitle: str | None = Field(default=None, max_length=512)


class StatusPageUpdate(BaseModel):
    slug: str | None = Field(
        default=None, min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    title: str | None = Field(default=None, max_length=255)
    subtitle: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


def _monitor_state(latest: Check | None) -> dict[str, object]:
    if latest is None:
        return {"state": "unknown"}
    return {
        "state": "up" if latest.success else "down",
        "reason": latest.reason,
        "latency_ms": latest.latency_ms,
        "checked_at": latest.checked_at.isoformat() if latest.checked_at else None,
    }


def _status_item(
    monitor: Monitor, latest: Check | None, incidents: Sequence[Incident]
) -> dict[str, object]:
    """Build one public status-page monitor entry (#33/#44).

    Extracted so the "never expose the raw target" rule is unit-testable: the
    HTML page omits ``target`` and the JSON view must too — the entry carries the
    name, kind, live state, and recent incidents, but never the origin URL.
    """
    return {
        "name": monitor.name,
        "check_kind": monitor.check_kind,
        **_monitor_state(latest),
        "incidents": [
            {
                "opened_at": i.opened_at.isoformat() if i.opened_at else None,
                "closed_at": i.closed_at.isoformat() if i.closed_at else None,
                "open_reason": i.open_reason,
            }
            for i in incidents
        ],
    }


async def _render_page(session: AsyncSession, page: StatusPage) -> dict[str, object]:
    """Build the public page payload from opted-in Monitors + Incidents."""
    monitors = (
        (
            await session.execute(
                select(Monitor)
                .where(Monitor.user_id == page.user_id, Monitor.show_on_status_page.is_(True))
                .order_by(Monitor.id)
            )
        )
        .scalars()
        .all()
    )

    items: list[dict[str, object]] = []
    for m in monitors:
        latest = (
            (
                await session.execute(
                    select(Check)
                    .where(Check.monitor_id == m.id)
                    .order_by(desc(Check.checked_at))
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        # Recent incidents (last 30 days) for history.
        since = datetime.now(UTC) - timedelta(days=30)
        incs = (
            (
                await session.execute(
                    select(Incident)
                    .where(Incident.monitor_id == m.id, Incident.opened_at >= since)
                    .order_by(desc(Incident.opened_at))
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        items.append(_status_item(m, latest, incs))
    return {
        "slug": page.slug,
        "title": page.title,
        "subtitle": page.subtitle,
        "updated_at": datetime.now(UTC).isoformat(),
        "monitors": items,
    }


@public_router.get("/{slug}")
async def public_status_page(
    slug: str,
    response: Response,
    request: Request,
    session: SessionDep,
) -> Any:
    """The public, unauthenticated, cached status page.

    Renders a human-readable HTML page by default; returns JSON if
    ``?format=json`` or ``Accept: application/json``. Cache-Control headers keep
    traffic spikes off the DB (ADR-0009).
    """
    page = await session.scalar(select(StatusPage).where(StatusPage.slug == slug))
    if page is None or not page.enabled:
        raise HTTPException(status_code=404, detail="status page not found")
    response.headers["Cache-Control"] = f"public, max-age={CACHE_TTL_S}"
    data = await _render_page(session, page)

    accept = request.headers.get("accept", "")
    want_json = request.query_params.get("format") == "json" or "application/json" in accept
    if want_json:
        return data
    from fastapi.templating import Jinja2Templates

    templates = Jinja2Templates(directory="templates")
    return templates.TemplateResponse(request, "public_status.html", data)


@config_router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_status_page(
    body: StatusPageCreate, user: ActiveUser, session: SessionDep
) -> dict[str, object]:
    existing = await session.scalar(select(StatusPage).where(StatusPage.user_id == user.id))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "status page already exists")
    slug_taken = await session.scalar(select(StatusPage).where(StatusPage.slug == body.slug))
    if slug_taken is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug already taken")
    page = StatusPage(user_id=user.id, slug=body.slug, title=body.title, subtitle=body.subtitle)
    session.add(page)
    await session.commit()
    await session.refresh(page)
    return {"id": page.id, "slug": page.slug, "title": page.title}


@config_router.get("", response_model=dict)
async def get_status_page(user: ActiveUser, session: SessionDep) -> dict[str, object]:
    page = await session.scalar(select(StatusPage).where(StatusPage.user_id == user.id))
    if page is None:
        raise HTTPException(status_code=404, detail="no status page; create one first")
    return {
        "id": page.id,
        "slug": page.slug,
        "title": page.title,
        "subtitle": page.subtitle,
        "enabled": page.enabled,
    }


@config_router.patch("", response_model=dict)
async def update_status_page(
    body: StatusPageUpdate, user: ActiveUser, session: SessionDep
) -> dict[str, object]:
    page = await session.scalar(select(StatusPage).where(StatusPage.user_id == user.id))
    if page is None:
        raise HTTPException(status_code=404, detail="no status page; create one first")
    data = body.model_dump(exclude_unset=True)
    if "slug" in data and data["slug"] is not None and data["slug"] != page.slug:
        slug_taken = await session.scalar(select(StatusPage).where(StatusPage.slug == data["slug"]))
        if slug_taken is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "slug already taken")
    for k, v in data.items():
        setattr(page, k, v)
    await session.commit()
    return {"id": page.id, "slug": page.slug, "title": page.title}
