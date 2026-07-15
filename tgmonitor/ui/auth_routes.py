"""Auth UI routes — server-rendered HTML forms for signup/login/verify/reset/logout.

These are thin HTML wrappers around the same auth primitives the JSON API uses
(password hashing, token creation, email sending). On successful login/signup,
the session token is set in a cookie (``tgm_session``) so HTML pages are
auth-gated without a Bearer header.

Each route renders a Jinja2 template and shows inline errors.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.auth.email import send_reset_email, send_verification_email
from tgmonitor.auth.ratelimit import client_ip_from_request, get_email_limiter, get_ip_limiter
from tgmonitor.auth.tokens import (
    create_purpose_token,
    create_session_token,
    decode_token,
    hash_password,
    verify_password,
)
from tgmonitor.db import get_session
from tgmonitor.models import User
from tgmonitor.ui.session import COOKIE_NAME

router = APIRouter(prefix="/ui", tags=["ui-auth"], include_in_schema=False)
templates = Jinja2Templates(directory="templates")

MIN_PASSWORD_LEN = 8

# A dummy argon2 hash used to keep login timing constant when the email is not
# registered (#29): a missing User otherwise short-circuits before the password
# hash check, creating a timing oracle.
_DUMMY_HASH = hash_password("constant-time-dummy-do-not-use")

# The template base checks ``current_user`` to decide whether to show the nav.
templates.env.globals["current_user"] = None


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # Caddy terminates TLS upstream; cookie travels over HTTPS to the proxy.
        max_age=60 * 60 * 24 * 14,  # 14 days, matching session_expire_minutes
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


def _client_ip(request: Request) -> str:
    return client_ip_from_request(request)


def _render(
    request: Request, name: str, ctx: dict[str, Any] | None = None, status_code: int = 200
) -> Any:
    """Render a template with the standard context (request injected by the new API)."""
    base: dict[str, Any] = {"current_user": None}
    if ctx:
        base.update(ctx)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


@router.get("/")
async def root_redirect(request: Request) -> RedirectResponse:
    """Logged-out → login; logged-in → dashboard. Fixes the bare-404 at /."""
    token = request.cookies.get(COOKIE_NAME)
    if token and decode_token(token, expected_purpose="session"):
        return RedirectResponse("/ui/monitors", status_code=302)
    return RedirectResponse("/ui/login", status_code=302)


@router.get("/login")
async def login_form(request: Request) -> Any:
    return _render(request, "login.html", {"email": "", "error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    if not await get_ip_limiter().check(_client_ip(request)):
        return _render(
            request,
            "login.html",
            {"email": email, "error": "Too many attempts. Please wait a few minutes."},
            status_code=429,
        )
    # Apply the per-email limiter too (#29): the UI login only checked the IP
    # limiter, so a distributed set of IPs could brute-force one email.
    if not await get_email_limiter().check(email.lower()):
        return _render(
            request,
            "login.html",
            {"email": email, "error": "Too many attempts for this email. Please wait."},
            status_code=429,
        )
    user = await session.scalar(select(User).where(User.email == email.lower()))
    # Constant-time password check for a missing User (#29): run a real verify
    # against a dummy hash so timing does not branch on user existence. The
    # naive `user is None or not verify(...)` short-circuits past the hash check.
    stored_hash = user.password_hash if user is not None else _DUMMY_HASH
    password_ok = verify_password(password, stored_hash)
    if user is None or not password_ok:
        return _render(
            request,
            "login.html",
            {"email": email, "error": "Invalid email or password."},
            status_code=401,
        )
    if not user.is_active:
        return _render(
            request,
            "login.html",
            {
                "email": email,
                "error": "Account not verified. Check your email for the verification link.",
            },
            status_code=403,
        )
    response = RedirectResponse("/ui/monitors", status_code=302)
    _set_session_cookie(response, create_session_token(user.id))
    return response


@router.get("/signup")
async def signup_form(request: Request) -> Any:
    return _render(request, "signup.html", {"email": "", "error": None, "success": None})


@router.post("/signup")
async def signup_submit(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    if len(password) < MIN_PASSWORD_LEN:
        return _render(
            request,
            "signup.html",
            {
                "email": email,
                "error": f"Password must be at least {MIN_PASSWORD_LEN} characters.",
                "success": None,
            },
            status_code=400,
        )
    if not await get_ip_limiter().check(_client_ip(request)):
        return _render(
            request,
            "signup.html",
            {"email": email, "error": "Too many attempts from this IP.", "success": None},
            status_code=429,
        )
    existing = await session.scalar(select(User).where(User.email == email.lower()))
    if existing is not None:
        return _render(
            request,
            "signup.html",
            {
                "email": email,
                "error": "An account with this email already exists.",
                "success": None,
            },
            status_code=409,
        )
    user = User(email=email.lower(), password_hash=hash_password(password), is_active=False)
    session.add(user)
    await session.flush()
    token, jti = create_purpose_token(user.id, "verify")
    user.verify_token_jti = jti
    await session.commit()
    send_verification_email(user.email, token)
    return _render(
        request,
        "signup.html",
        {
            "email": email,
            "error": None,
            "success": "Account created! Check your email for a verification link to activate it.",
        },
    )


@router.get("/verify")
async def verify_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    token: str = "",
) -> Any:
    payload = decode_token(token, expected_purpose="verify")
    if payload is None:
        return _render(
            request,
            "verify.html",
            {"success": None, "error": "This verification link is invalid or expired."},
        )
    user = await session.get(User, int(payload.sub))
    if user is None or user.verify_token_jti != payload.jti:
        return _render(
            request,
            "verify.html",
            {
                "success": None,
                "error": "This verification link has already been used or is invalid.",
            },
        )
    user.is_active = True
    user.verify_token_jti = None
    await session.commit()
    return _render(
        request,
        "verify.html",
        {"success": f"Email verified! {user.email} is now active.", "error": None},
    )


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse("/ui/login", status_code=302)
    _clear_session_cookie(response)
    return response


@router.get("/reset/request")
async def reset_request_form(request: Request) -> Any:
    return _render(request, "reset_request.html", {"success": None, "error": None})


@router.post("/reset/request")
async def reset_request_submit(
    request: Request,
    email: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    # Enforce the limiter (#29): the result was discarded, so reset requests
    # were unthrottled (each also rotates the reset token, invalidating prior
    # legitimate links).
    if not await get_ip_limiter().check(_client_ip(request)):
        return _render(
            request,
            "reset_request.html",
            {"success": None, "error": "Too many attempts. Please wait a few minutes."},
            status_code=429,
        )
    if not await get_email_limiter().check(email.lower()):
        return _render(
            request,
            "reset_request.html",
            {"success": None, "error": "Too many attempts for this email. Please wait."},
            status_code=429,
        )
    user = await session.scalar(select(User).where(User.email == email.lower()))
    if user is not None:
        token, jti = create_purpose_token(user.id, "reset")
        user.reset_token_jti = jti
        await session.commit()
        send_reset_email(user.email, token)
    # Always show the same message — never reveal whether an email is registered.
    return _render(
        request,
        "reset_request.html",
        {"success": "If the email is registered, a reset link has been sent.", "error": None},
    )


@router.get("/reset")
async def reset_confirm_form(request: Request, token: str = "") -> Any:
    payload = decode_token(token, expected_purpose="reset")
    if payload is None:
        return _render(
            request,
            "reset_confirm.html",
            {"token": "", "success": None, "error": "This reset link is invalid or expired."},
        )
    return _render(request, "reset_confirm.html", {"token": token, "success": None, "error": None})


@router.post("/reset/confirm")
async def reset_confirm_submit(
    request: Request,
    token: Annotated[str, Form()],
    password: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    if len(password) < MIN_PASSWORD_LEN:
        return _render(
            request,
            "reset_confirm.html",
            {
                "token": token,
                "success": None,
                "error": f"Password must be at least {MIN_PASSWORD_LEN} characters.",
            },
            status_code=400,
        )
    payload = decode_token(token, expected_purpose="reset")
    if payload is None:
        return _render(
            request,
            "reset_confirm.html",
            {"token": "", "success": None, "error": "This reset link is invalid or expired."},
            status_code=400,
        )
    user = await session.get(User, int(payload.sub))
    if user is None or user.reset_token_jti != payload.jti:
        return _render(
            request,
            "reset_confirm.html",
            {"token": "", "success": None, "error": "This reset link has already been used."},
            status_code=400,
        )
    user.password_hash = hash_password(password)
    user.reset_token_jti = None
    await session.commit()
    # Redirect to login with a success indicator.
    return _render(
        request,
        "login.html",
        {"email": user.email, "error": None, "success": "Password reset! You can now log in."},
    )
