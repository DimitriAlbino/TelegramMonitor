"""Scheduled Report configuration API (ADR-0006 kind #1).

A User configures a periodical Report (daily/weekly/monthly + delivery time).
The worker's scheduler claims due reports by next_run_at, renders the digest,
and delivers it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.auth.dependencies import ActiveUser
from tgmonitor.db import get_session
from tgmonitor.models import Monitor
from tgmonitor.report_model import Report
from tgmonitor.reports import PERIOD_DAYS, next_run_for_delivery_time

router = APIRouter(prefix="/reports", tags=["reports"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ReportCreate(BaseModel):
    cadence: Literal["daily", "weekly", "monthly"]
    delivery_time: str = Field(default="08:00", pattern=r"^\d{2}:\d{2}$")
    timezone: str | None = None
    monitor_id: int | None = None


class ReportOut(BaseModel):
    id: int
    cadence: str
    delivery_time: str
    timezone: str | None
    monitor_id: int | None
    next_run_at: str


@router.post("", response_model=ReportOut, status_code=status.HTTP_201_CREATED)
async def create_report(body: ReportCreate, user: ActiveUser, session: SessionDep) -> ReportOut:
    # Validate monitor_id ownership at creation (#31): a Report must not be
    # scoped to another tenant's monitor.
    if body.monitor_id is not None:
        owned = await session.scalar(
            select(Monitor.id).where(Monitor.id == body.monitor_id, Monitor.user_id == user.id)
        )
        if owned is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "monitor not found")
    cadence_days = PERIOD_DAYS.get(body.cadence, 1)
    report = Report(
        user_id=user.id,
        monitor_id=body.monitor_id,
        cadence=body.cadence,
        delivery_time=body.delivery_time,
        timezone=body.timezone,
        # Schedule at the configured delivery_time/timezone, not now+cadence (#31).
        next_run_at=next_run_for_delivery_time(
            body.delivery_time, body.timezone, cadence_days=cadence_days
        ),
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return ReportOut(
        id=report.id,
        cadence=report.cadence,
        delivery_time=report.delivery_time,
        timezone=report.timezone,
        monitor_id=report.monitor_id,
        next_run_at=report.next_run_at.isoformat() if report.next_run_at else "",
    )


@router.get("", response_model=list[ReportOut])
async def list_reports(
    user: ActiveUser, session: SessionDep, limit: int = Query(default=50, ge=1, le=200)
) -> list[ReportOut]:
    rows = (
        (
            await session.execute(
                select(Report).where(Report.user_id == user.id).order_by(Report.id).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        ReportOut(
            id=r.id,
            cadence=r.cadence,
            delivery_time=r.delivery_time,
            timezone=r.timezone,
            monitor_id=r.monitor_id,
            next_run_at=r.next_run_at.isoformat() if r.next_run_at else "",
        )
        for r in rows
    ]


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report(report_id: int, user: ActiveUser, session: SessionDep) -> None:
    report = await session.get(Report, report_id)
    if report is None or report.user_id != user.id:
        raise HTTPException(status_code=404, detail="report not found")
    await session.delete(report)
    await session.commit()
