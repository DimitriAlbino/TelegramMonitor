"""Monitor CRUD API routes.

Authenticated, ownership-scoped endpoints for creating, reading, updating,
deleting, pausing, and resuming Monitors. Two Check kinds are supported:
HTTP and TCP (CONTEXT.md Check Kind).

Abuse prevention (ADR-0011): a per-user Monitor cap (``MAX_MONITORS_PER_USER``)
and a minimum check interval floor (``MIN_CHECK_INTERVAL_S``) are enforced on
create/update.

Vocabulary is normative (CONTEXT.md): the entity is a ``Monitor``; ``Paused``
stops the engine from running Checks (distinct from the later ``Muted``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.auth.dependencies import ActiveUser
from tgmonitor.config import get_settings
from tgmonitor.db import get_session
from tgmonitor.incident_model import Incident
from tgmonitor.models import Check, Monitor, User

router = APIRouter(prefix="/monitors", tags=["monitors"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

VALID_KINDS = ("http", "tcp", "api_content")


class HttpConfigIn(BaseModel):
    expected_status: int = Field(default=200, ge=100, le=599)
    body_contains: str | None = None
    max_latency_ms: int | None = Field(default=None, ge=1)
    timeout_s: float = Field(default=10.0, ge=0.5, le=60.0)


class TcpConfigIn(BaseModel):
    timeout_s: float = Field(default=5.0, ge=0.5, le=60.0)


class MonitorBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    check_kind: Literal["http", "tcp", "api_content"]
    target: str = Field(min_length=1, max_length=2048)
    interval_s: int = Field(ge=1)
    failure_threshold: int = Field(default=3, ge=1)
    recovery_threshold: int = Field(default=2, ge=1)
    critical: bool = False
    show_on_status_page: bool = False
    # Kind-specific knobs. Required keys depend on check_kind.
    expected_status: int | None = Field(default=None, ge=100, le=599)
    body_contains: str | None = None
    max_latency_ms: int | None = Field(default=None, ge=1)
    timeout_s: float | None = Field(default=None, ge=0.5, le=60.0)
    follow_redirects: bool = False
    # api_content knobs (JSON field path + alarm keyword).
    json_field_path: str | None = None
    json_keyword: str | None = None


class MonitorCreate(MonitorBase):
    @model_validator(mode="after")
    def validate_kind_fields(self) -> MonitorCreate:
        if self.check_kind == "tcp" and ":" not in self.target:
            raise ValueError("tcp target must be host:port")
        return self


class MonitorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    target: str | None = Field(default=None, min_length=1, max_length=2048)
    interval_s: int | None = Field(default=None, ge=1)
    failure_threshold: int | None = Field(default=None, ge=1)
    recovery_threshold: int | None = Field(default=None, ge=1)
    critical: bool | None = None
    show_on_status_page: bool | None = None
    expected_status: int | None = Field(default=None, ge=100, le=599)
    body_contains: str | None = None
    max_latency_ms: int | None = Field(default=None, ge=1)
    timeout_s: float | None = Field(default=None, ge=0.5, le=60.0)
    follow_redirects: bool | None = None
    json_field_path: str | None = None
    json_keyword: str | None = None


class MonitorOut(BaseModel):
    id: int
    name: str
    check_kind: str
    target: str
    interval_s: int
    failure_threshold: int
    recovery_threshold: int
    critical: bool
    show_on_status_page: bool
    paused: bool
    muted: bool
    next_check_at: str | None = None
    config: dict[str, Any]
    last_result: dict[str, Any] | None = None


def _build_config(m: MonitorBase | MonitorUpdate) -> dict[str, Any]:
    """Extract kind-specific knobs into the JSONB config column."""
    cfg: dict[str, Any] = {}
    if m.expected_status is not None:
        cfg["expected_status"] = m.expected_status
    if m.body_contains is not None:
        cfg["body_contains"] = m.body_contains
    if m.max_latency_ms is not None:
        cfg["max_latency_ms"] = m.max_latency_ms
    if m.timeout_s is not None:
        # HTTP and TCP both read timeout_s / tcp_timeout_s respectively; store
        # both forms so the engine's claim query picks the right one.
        cfg["timeout_s"] = m.timeout_s
        cfg["tcp_timeout_s"] = m.timeout_s
    if getattr(m, "follow_redirects", None) is not None:
        cfg["follow_redirects"] = m.follow_redirects
    if getattr(m, "json_field_path", None) is not None:
        cfg["json_field_path"] = m.json_field_path
    if getattr(m, "json_keyword", None) is not None:
        cfg["json_keyword"] = m.json_keyword
    return cfg


def _to_out(m: Monitor, last_result: Check | None) -> MonitorOut:
    return MonitorOut(
        id=m.id,
        name=m.name,
        check_kind=m.check_kind,
        target=m.target,
        interval_s=m.interval_s,
        failure_threshold=m.failure_threshold,
        recovery_threshold=m.recovery_threshold,
        critical=m.critical,
        show_on_status_page=m.show_on_status_page,
        paused=m.paused,
        muted=m.muted,
        next_check_at=m.next_check_at.isoformat() if m.next_check_at else None,
        config=dict(m.config or {}),
        last_result=_result_brief(last_result),
    )


def _result_brief(c: Check | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {
        "success": c.success,
        "checked_at": c.checked_at.isoformat() if c.checked_at else None,
        "latency_ms": c.latency_ms,
        "reason": c.reason,
    }


async def _get_owned(monitor_id: int, user: User, session: AsyncSession) -> Monitor:
    m = await session.get(Monitor, monitor_id)
    if m is None or m.user_id != user.id:
        raise HTTPException(status_code=404, detail="monitor not found")
    return m


def _enforce_interval(interval_s: int) -> None:
    floor = get_settings().min_check_interval_s
    if interval_s < floor:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"interval_s {interval_s} is below the minimum floor {floor}",
        )


async def _enforce_cap(user: User, session: AsyncSession, exclude_id: int | None = None) -> None:
    cap = get_settings().max_monitors_per_user
    stmt = select(func.count(Monitor.id)).where(Monitor.user_id == user.id)
    if exclude_id is not None:
        stmt = stmt.where(Monitor.id != exclude_id)
    count = (await session.execute(stmt)).scalar_one()
    if count >= cap:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"monitor cap reached ({cap} per user)",
        )


@router.post("", response_model=MonitorOut, status_code=status.HTTP_201_CREATED)
async def create_monitor(body: MonitorCreate, user: ActiveUser, session: SessionDep) -> MonitorOut:
    _enforce_interval(body.interval_s)
    await _enforce_cap(user, session)
    monitor = Monitor(
        user_id=user.id,
        name=body.name,
        check_kind=body.check_kind,
        target=body.target,
        interval_s=body.interval_s,
        failure_threshold=body.failure_threshold,
        recovery_threshold=body.recovery_threshold,
        critical=body.critical,
        show_on_status_page=body.show_on_status_page,
        config=_build_config(body),
    )
    session.add(monitor)
    await session.commit()
    await session.refresh(monitor)
    return _to_out(monitor, None)


@router.get("", response_model=list[MonitorOut])
async def list_monitors(
    user: ActiveUser,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[MonitorOut]:
    """List the user's Monitors with their latest Result as ``last_result``."""
    stmt = select(Monitor).where(Monitor.user_id == user.id).order_by(Monitor.id).limit(limit)
    monitors = (await session.execute(stmt)).scalars().all()

    # Latest Result per monitor: a subquery of max(checked_at) per monitor joined
    # back to the full row. (SQLAlchemy 2.0 dropped the public distinct_on method
    # on Select; this join form is portable and equally cheap.)
    latest: dict[int, Check] = {}
    if monitors:
        ids = [m.id for m in monitors]
        max_subq = (
            select(Check.monitor_id.label("mid"), func.max(Check.checked_at).label("mcat"))
            .where(Check.monitor_id.in_(ids))
            .group_by(Check.monitor_id)
            .subquery()
        )
        latest_stmt = select(Check).join(
            max_subq,
            (Check.monitor_id == max_subq.c.mid) & (Check.checked_at == max_subq.c.mcat),
        )
        latest = {c.monitor_id: c for c in (await session.execute(latest_stmt)).scalars()}
    return [_to_out(m, latest.get(m.id)) for m in monitors]


@router.get("/{monitor_id}", response_model=MonitorOut)
async def get_monitor(monitor_id: int, user: ActiveUser, session: SessionDep) -> MonitorOut:
    monitor = await _get_owned(monitor_id, user, session)
    latest_stmt = (
        select(Check)
        .where(Check.monitor_id == monitor_id)
        .order_by(desc(Check.checked_at))
        .limit(1)
    )
    latest = (await session.execute(latest_stmt)).scalars().first()
    return _to_out(monitor, latest)


@router.patch("/{monitor_id}", response_model=MonitorOut)
async def update_monitor(
    monitor_id: int, body: MonitorUpdate, user: ActiveUser, session: SessionDep
) -> MonitorOut:
    monitor = await _get_owned(monitor_id, user, session)
    data = body.model_dump(exclude_unset=True)
    if "interval_s" in data and data["interval_s"] is not None:
        _enforce_interval(data["interval_s"])
    if (
        "target" in data
        and data["target"] is not None
        and monitor.check_kind == "tcp"
        and ":" not in data["target"]
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tcp target must be host:port")
    # Merge kind-specific knobs into the existing config.
    config_keys = {
        "expected_status",
        "body_contains",
        "max_latency_ms",
        "timeout_s",
        "follow_redirects",
        "json_field_path",
        "json_keyword",
    }
    cfg = dict(monitor.config or {})
    for k in config_keys:
        if k in data:
            if data[k] is None:
                cfg.pop(k, None)
            else:
                cfg[k] = data[k]
                if k == "timeout_s":
                    cfg["tcp_timeout_s"] = data[k]
    monitor.config = cfg
    for k in (
        "name",
        "target",
        "interval_s",
        "failure_threshold",
        "recovery_threshold",
        "critical",
        "show_on_status_page",
    ):
        if k in data and data[k] is not None:
            setattr(monitor, k, data[k])
    await session.commit()
    await session.refresh(monitor)
    return _to_out(monitor, None)


@router.delete("/{monitor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_monitor(monitor_id: int, user: ActiveUser, session: SessionDep) -> None:
    monitor = await _get_owned(monitor_id, user, session)
    await session.delete(monitor)
    await session.commit()


@router.post("/{monitor_id}/pause", response_model=MonitorOut)
async def pause_monitor(monitor_id: int, user: ActiveUser, session: SessionDep) -> MonitorOut:
    """Pause: the engine stops running Checks for this Monitor.

    CONTEXT.md: Paused means no execution, no Results — distinct from Muted.
    The engine's claim query filters ``paused = false``, so a paused Monitor is
    simply never selected.
    """
    monitor = await _get_owned(monitor_id, user, session)
    monitor.paused = True
    await session.commit()
    await session.refresh(monitor)
    return _to_out(monitor, None)


@router.post("/{monitor_id}/resume", response_model=MonitorOut)
async def resume_monitor(monitor_id: int, user: ActiveUser, session: SessionDep) -> MonitorOut:
    """Resume: Checks run again on the next tick."""
    monitor = await _get_owned(monitor_id, user, session)
    monitor.paused = False
    # Reset next_check_at so the Monitor is immediately due.
    from tgmonitor.models import utcnow

    monitor.next_check_at = utcnow()
    await session.commit()
    await session.refresh(monitor)
    return _to_out(monitor, None)


class IncidentOut(BaseModel):
    id: int
    monitor_id: int
    opened_at: str
    closed_at: str | None = None
    open_reason: str
    close_reason: str | None = None
    outcome: str | None = None


def _incident_to_out(inc: Incident) -> IncidentOut:
    return IncidentOut(
        id=inc.id,
        monitor_id=inc.monitor_id,
        opened_at=inc.opened_at.isoformat() if inc.opened_at else "",
        closed_at=inc.closed_at.isoformat() if inc.closed_at else None,
        open_reason=inc.open_reason,
        close_reason=inc.close_reason,
        outcome=inc.outcome,
    )


@router.get("/{monitor_id}/incidents", response_model=list[IncidentOut])
async def list_incidents(
    monitor_id: int,
    user: ActiveUser,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[IncidentOut]:
    """List Incidents for a Monitor (newest first). Ownership-scoped."""
    await _get_owned(monitor_id, user, session)
    stmt = (
        select(Incident)
        .where(Incident.monitor_id == monitor_id)
        .order_by(Incident.opened_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_incident_to_out(i) for i in rows]


@router.post("/{monitor_id}/test-alert")
async def send_test_alert(
    monitor_id: int, user: ActiveUser, session: SessionDep
) -> dict[str, object]:
    """Send a synthetic test Alert to the user's linked Telegram chat.

    De-risks the delivery pipe (BotFather token, webhook TLS, chat-id binding)
    independently of the Incident state machine (T5). Requires a linked chat.
    """
    monitor = await _get_owned(monitor_id, user, session)
    # Reload the user to get the freshest telegram_chat_id.
    owner = await session.get(User, user.id)
    chat_id = owner.telegram_chat_id if owner else None
    if not chat_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "no linked Telegram chat; use the Link Telegram flow first",
        )
    from tgmonitor.telegram.client import NotificationChannel

    channel = NotificationChannel()
    text = f"🧪 <b>{monitor.name}</b> — this is a test alert. Delivery is working."
    delivered = await channel.send(chat_id, text)
    return {"delivered": delivered, "monitor_id": monitor.id}
