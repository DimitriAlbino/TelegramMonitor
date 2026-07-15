"""Authenticated web UI routes — monitors, settings, status-page.

All pages use the cookie-based session (``ActiveUserCookie``). They render the
data the JSON API exposes (monitors, incidents, results, reports) as HTML.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.auth.tokens import hash_password, verify_password
from tgmonitor.config import get_settings
from tgmonitor.db import get_session
from tgmonitor.incident_model import Incident
from tgmonitor.models import Check, Monitor, User
from tgmonitor.statuspage_model import StatusPage
from tgmonitor.ui.session import ActiveUserCookie

router = APIRouter(prefix="/ui", tags=["ui-app"], include_in_schema=False)
templates = Jinja2Templates(directory="templates")


def _render(
    request: Request, name: str, ctx: dict[str, Any] | None = None, status_code: int = 200
) -> Any:
    """Render a template, injecting the session user for the nav.

    The cookie-session dependency stores the resolved user on ``request.state``
    so it's available here without every route passing it explicitly. base.html
    checks ``current_user`` to show the nav links.
    """
    current_user = getattr(request.state, "user", None)
    base: dict[str, Any] = {"request": request, "current_user": current_user}
    if ctx:
        base.update(ctx)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


def _state_badge(monitor: Monitor, latest: Check | None) -> str:
    if monitor.paused:
        return "paused"
    if monitor.incident_status == "flapping":
        return "flapping"
    if monitor.incident_status == "down":
        return "down"
    if latest is None:
        return "unknown"
    return "up" if latest.success else "down"


# ==================== U2: Monitor dashboard ====================


@router.get("/monitors")
async def dashboard(
    request: Request,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    monitors = (
        (
            await session.execute(
                select(Monitor).where(Monitor.user_id == user.id).order_by(Monitor.id)
            )
        )
        .scalars()
        .all()
    )

    # Latest check per monitor for live state.
    latest_map: dict[int, Check] = {}
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
        if latest:
            latest_map[m.id] = latest

    rows = [
        {
            "monitor": m,
            "latest": latest_map.get(m.id),
            "badge": _state_badge(m, latest_map.get(m.id)),
        }
        for m in monitors
    ]
    return _render(
        request, "dashboard.html", {"rows": rows, "cap": get_settings().max_monitors_per_user}
    )


@router.get("/monitors/new")
async def create_monitor_form(request: Request, user: ActiveUserCookie) -> Any:
    return _render(request, "monitor_form.html", {"m": None, "error": None})


@router.post("/monitors/new")
async def create_monitor_submit(
    request: Request,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
    name: Annotated[str, Form()] = "",
    check_kind: Annotated[str, Form()] = "http",
    target: Annotated[str, Form()] = "",
    interval_s: Annotated[int, Form()] = 30,
    failure_threshold: Annotated[int, Form()] = 3,
    recovery_threshold: Annotated[int, Form()] = 2,
    expected_status: Annotated[int, Form()] = 200,
    body_contains: Annotated[str, Form()] = "",
    max_latency_ms: Annotated[int | None, Form()] = None,
    timeout_s: Annotated[float, Form()] = 10.0,
    follow_redirects: Annotated[bool, Form()] = False,
    json_field_path: Annotated[str, Form()] = "",
    json_keyword: Annotated[str, Form()] = "",
    critical: Annotated[bool, Form()] = False,
    show_on_status_page: Annotated[bool, Form()] = False,
) -> Any:
    if not name or not target:
        return _render(
            request, "monitor_form.html", {"m": None, "error": "Name and target are required."}
        )

    # Enforce the cap.
    from sqlalchemy import func

    count = (
        await session.execute(select(func.count(Monitor.id)).where(Monitor.user_id == user.id))
    ).scalar_one()
    if count >= get_settings().max_monitors_per_user:
        return _render(
            request,
            "monitor_form.html",
            {"m": None, "error": f"Monitor cap reached ({get_settings().max_monitors_per_user})."},
        )
    if interval_s < get_settings().min_check_interval_s:
        return _render(
            request,
            "monitor_form.html",
            {
                "m": None,
                "error": f"Interval must be at least {get_settings().min_check_interval_s}s.",
            },
        )

    config: dict[str, Any] = {
        "timeout_s": timeout_s,
        "tcp_timeout_s": timeout_s,
        "follow_redirects": follow_redirects,
    }
    if check_kind == "http":
        config["expected_status"] = expected_status
        if body_contains:
            config["body_contains"] = body_contains
        if max_latency_ms:
            config["max_latency_ms"] = max_latency_ms
    if check_kind == "api_content":
        if json_field_path:
            config["json_field_path"] = json_field_path
        if json_keyword:
            config["json_keyword"] = json_keyword

    monitor = Monitor(
        user_id=user.id,
        name=name,
        check_kind=check_kind,
        target=target,
        interval_s=interval_s,
        failure_threshold=failure_threshold,
        recovery_threshold=recovery_threshold,
        critical=critical,
        show_on_status_page=show_on_status_page,
        config=config,
    )
    session.add(monitor)
    await session.commit()
    return RedirectResponse("/ui/monitors", status_code=302)


# ==================== U3: Monitor detail ====================


async def _get_owned(monitor_id: int, user: User, session: AsyncSession) -> Monitor:
    m = await session.get(Monitor, monitor_id)
    if m is None or m.user_id != user.id:
        raise HTTPException(status_code=404, detail="monitor not found")
    return m


@router.get("/monitors/{monitor_id}")
async def monitor_detail(
    request: Request,
    monitor_id: int,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    monitor = await _get_owned(monitor_id, user, session)
    results = (
        (
            await session.execute(
                select(Check)
                .where(Check.monitor_id == monitor_id)
                .order_by(desc(Check.checked_at))
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    incidents = (
        (
            await session.execute(
                select(Incident)
                .where(Incident.monitor_id == monitor_id)
                .order_by(desc(Incident.opened_at))
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    latest = results[0] if results else None
    return _render(
        request,
        "monitor_detail.html",
        {
            "m": monitor,
            "results": results,
            "incidents": incidents,
            "badge": _state_badge(monitor, latest),
            "telegram_linked": bool(user.telegram_chat_id),
        },
    )


@router.get("/monitors/{monitor_id}/edit")
async def edit_monitor_form(
    request: Request,
    monitor_id: int,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    monitor = await _get_owned(monitor_id, user, session)
    return _render(request, "monitor_form.html", {"m": monitor, "error": None})


@router.post("/monitors/{monitor_id}/edit")
async def edit_monitor_submit(
    request: Request,
    monitor_id: int,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
    name: Annotated[str, Form()] = "",
    target: Annotated[str, Form()] = "",
    interval_s: Annotated[int, Form()] = 30,
    failure_threshold: Annotated[int, Form()] = 3,
    recovery_threshold: Annotated[int, Form()] = 2,
    expected_status: Annotated[int, Form()] = 200,
    body_contains: Annotated[str, Form()] = "",
    max_latency_ms: Annotated[int | None, Form()] = None,
    timeout_s: Annotated[float, Form()] = 10.0,
    follow_redirects: Annotated[bool, Form()] = False,
    json_field_path: Annotated[str, Form()] = "",
    json_keyword: Annotated[str, Form()] = "",
    critical: Annotated[bool, Form()] = False,
    show_on_status_page: Annotated[bool, Form()] = False,
) -> Any:
    monitor = await _get_owned(monitor_id, user, session)
    if not name or not target:
        return _render(
            request, "monitor_form.html", {"m": monitor, "error": "Name and target are required."}
        )
    if interval_s < get_settings().min_check_interval_s:
        return _render(
            request,
            "monitor_form.html",
            {
                "m": monitor,
                "error": f"Interval must be at least {get_settings().min_check_interval_s}s.",
            },
        )
    monitor.name = name
    monitor.target = target
    monitor.interval_s = interval_s
    monitor.failure_threshold = failure_threshold
    monitor.recovery_threshold = recovery_threshold
    monitor.critical = critical
    monitor.show_on_status_page = show_on_status_page
    config: dict[str, Any] = {"timeout_s": timeout_s, "tcp_timeout_s": timeout_s}
    if monitor.check_kind == "http":
        config["expected_status"] = expected_status
        if body_contains:
            config["body_contains"] = body_contains
        if max_latency_ms:
            config["max_latency_ms"] = max_latency_ms
    monitor.config = config
    await session.commit()
    return RedirectResponse(f"/ui/monitors/{monitor_id}", status_code=302)


@router.post("/monitors/{monitor_id}/pause")
async def pause_monitor(
    monitor_id: int, user: ActiveUserCookie, session: Annotated[AsyncSession, Depends(get_session)]
) -> RedirectResponse:
    monitor = await _get_owned(monitor_id, user, session)
    monitor.paused = True
    await session.commit()
    return RedirectResponse("/ui/monitors", status_code=302)


@router.post("/monitors/{monitor_id}/resume")
async def resume_monitor(
    monitor_id: int, user: ActiveUserCookie, session: Annotated[AsyncSession, Depends(get_session)]
) -> RedirectResponse:
    from tgmonitor.models import utcnow

    monitor = await _get_owned(monitor_id, user, session)
    monitor.paused = False
    monitor.next_check_at = utcnow()
    await session.commit()
    return RedirectResponse("/ui/monitors", status_code=302)


@router.post("/monitors/{monitor_id}/delete")
async def delete_monitor(
    monitor_id: int, user: ActiveUserCookie, session: Annotated[AsyncSession, Depends(get_session)]
) -> RedirectResponse:
    monitor = await _get_owned(monitor_id, user, session)
    await session.delete(monitor)
    await session.commit()
    return RedirectResponse("/ui/monitors", status_code=302)


@router.post("/monitors/{monitor_id}/test-alert")
async def test_alert(
    request: Request,
    monitor_id: int,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    monitor = await _get_owned(monitor_id, user, session)
    owner = await session.get(User, user.id)
    chat_id = owner.telegram_chat_id if owner else None
    if not chat_id:
        return _render(
            request,
            "monitor_detail.html",
            {
                "m": monitor,
                "results": [],
                "incidents": [],
                "badge": "unknown",
                "telegram_linked": False,
                "error": "No linked Telegram chat. Set your chat ID in Settings first.",
            },
        )
    from tgmonitor.telegram.client import NotificationChannel

    channel = NotificationChannel()
    await channel.send(chat_id, f"🧪 <b>{monitor.name}</b> — test alert from the web UI.")
    return RedirectResponse(f"/ui/monitors/{monitor_id}", status_code=302)


# ==================== U4: Link Telegram ====================


# ==================== U5: Status Page config ====================


@router.get("/status-page")
async def status_page_config(
    request: Request, user: ActiveUserCookie, session: Annotated[AsyncSession, Depends(get_session)]
) -> Any:
    page = await session.scalar(select(StatusPage).where(StatusPage.user_id == user.id))
    monitors = (
        (
            await session.execute(
                select(Monitor).where(Monitor.user_id == user.id).order_by(Monitor.id)
            )
        )
        .scalars()
        .all()
    )
    return _render(
        request,
        "status_page.html",
        {"page": page, "monitors": monitors, "error": None, "success": None},
    )


@router.post("/status-page")
async def status_page_create(
    request: Request,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
    slug: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "Status",
    subtitle: Annotated[str, Form()] = "",
) -> Any:
    monitors = (
        (await session.execute(select(Monitor).where(Monitor.user_id == user.id))).scalars().all()
    )
    existing = await session.scalar(select(StatusPage).where(StatusPage.user_id == user.id))
    if existing:
        return _render(
            request,
            "status_page.html",
            {
                "page": existing,
                "monitors": monitors,
                "error": "You already have a status page.",
                "success": None,
            },
        )
    slug_taken = await session.scalar(select(StatusPage).where(StatusPage.slug == slug))
    if slug_taken:
        return _render(
            request,
            "status_page.html",
            {
                "page": None,
                "monitors": monitors,
                "error": "That slug is already taken.",
                "success": None,
            },
        )
    page = StatusPage(user_id=user.id, slug=slug, title=title, subtitle=subtitle or None)
    session.add(page)
    await session.commit()
    return RedirectResponse("/ui/status-page", status_code=302)


@router.post("/status-page/update")
async def status_page_update(
    request: Request,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
    title: Annotated[str, Form()] = "Status",
    subtitle: Annotated[str, Form()] = "",
) -> Any:
    page = await session.scalar(select(StatusPage).where(StatusPage.user_id == user.id))
    monitors = (
        (await session.execute(select(Monitor).where(Monitor.user_id == user.id))).scalars().all()
    )
    if page is None:
        return _render(
            request,
            "status_page.html",
            {
                "page": None,
                "monitors": monitors,
                "error": "No status page to update.",
                "success": None,
            },
        )
    page.title = title
    page.subtitle = subtitle or None
    await session.commit()
    return RedirectResponse("/ui/status-page", status_code=302)


@router.post("/monitors/{monitor_id}/toggle-status-page")
async def toggle_status_page(
    monitor_id: int,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
    show: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    monitor = await _get_owned(monitor_id, user, session)
    monitor.show_on_status_page = show
    await session.commit()
    return RedirectResponse("/ui/status-page", status_code=302)


# ==================== U6: Account settings ====================


@router.get("/settings")
async def settings_form(
    request: Request, user: ActiveUserCookie, session: Annotated[AsyncSession, Depends(get_session)]
) -> Any:
    owner = await session.get(User, user.id)
    return _render(
        request,
        "settings.html",
        {"owner": owner, "error": None, "success": None},
    )


@router.post("/settings/password")
async def change_password(
    request: Request,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
) -> Any:
    owner = await session.get(User, user.id)
    if owner is None:
        raise HTTPException(status_code=404, detail="user not found")
    if not verify_password(current_password, owner.password_hash):
        return _render(
            request,
            "settings.html",
            {"owner": owner, "error": "Current password is incorrect.", "success": None},
        )
    if len(new_password) < 8:
        return _render(
            request,
            "settings.html",
            {
                "owner": owner,
                "error": "New password must be at least 8 characters.",
                "success": None,
            },
        )
    owner.password_hash = hash_password(new_password)
    await session.commit()
    return _render(
        request,
        "settings.html",
        {"owner": owner, "error": None, "success": "Password changed."},
    )


@router.post("/settings/quiet-hours")
async def set_quiet_hours(
    request: Request,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
    quiet_hours_enabled: Annotated[bool, Form()] = False,
    quiet_hours_start: Annotated[str, Form()] = "",
    quiet_hours_end: Annotated[str, Form()] = "",
    quiet_hours_tz: Annotated[str, Form()] = "UTC",
) -> Any:
    owner = await session.get(User, user.id)
    if owner is None:
        raise HTTPException(status_code=404, detail="user not found")
    if quiet_hours_enabled and quiet_hours_start and quiet_hours_end:
        owner.quiet_hours_start = quiet_hours_start
        owner.quiet_hours_end = quiet_hours_end
        owner.quiet_hours_tz = quiet_hours_tz
    else:
        owner.quiet_hours_start = None
        owner.quiet_hours_end = None
        owner.quiet_hours_tz = None
    await session.commit()
    return _render(
        request,
        "settings.html",
        {"owner": owner, "error": None, "success": "Quiet hours updated."},
    )


@router.post("/settings/telegram-chat-id")
async def set_telegram_chat_id(
    request: Request,
    user: ActiveUserCookie,
    session: Annotated[AsyncSession, Depends(get_session)],
    telegram_chat_id: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
) -> Any:
    """Set or remove the user's Telegram chat ID — the Notification Channel target."""
    owner = await session.get(User, user.id)
    if owner is None:
        raise HTTPException(status_code=404, detail="user not found")

    if action == "remove":
        owner.telegram_chat_id = None
        await session.commit()
        return _render(
            request,
            "settings.html",
            {"owner": owner, "error": None, "success": "Telegram chat ID removed."},
        )

    chat_id = telegram_chat_id.strip()
    if not chat_id or not chat_id.isdigit() or len(chat_id) > 20:
        return _render(
            request,
            "settings.html",
            {
                "owner": owner,
                "error": "Chat ID must be numeric (message @userinfobot on Telegram to find yours).",
                "success": None,
            },
        )

    owner.telegram_chat_id = chat_id
    await session.commit()

    # Send one confirmation message to the chat as a cheap mistake/abuse signal.
    # Delivery failure is non-blocking — the ID is saved regardless.
    delivery_note = ""
    try:
        from tgmonitor.telegram.client import NotificationChannel

        channel = NotificationChannel()
        sent = await channel.send(
            chat_id,
            "This chat is now linked to your TelegramMonitor account. "
            "If this wasn't you, go to Settings and remove the chat ID.",
        )
        if not sent:
            delivery_note = " Chat ID saved, but the confirmation message could not be delivered — verify the ID is correct."
    except Exception:
        delivery_note = " Chat ID saved, but the confirmation message could not be delivered."

    return _render(
        request,
        "settings.html",
        {
            "owner": owner,
            "error": None,
            "success": f"Telegram chat ID saved ({chat_id}).{delivery_note}",
        },
    )
