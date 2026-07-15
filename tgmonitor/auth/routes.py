"""Auth API routes: signup, verify, login, logout, reset, change password.

Sessions are stateless JWTs; logout is therefore client-side (drop the token).
The routes are rate-limited per IP and per email (ADR-0011 abuse prevention).

CONTEXT.md vocabulary: the person is a ``User``; the session is a ``token``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tgmonitor.auth.dependencies import CurrentUser
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

router = APIRouter(prefix="/auth", tags=["auth"])

# Annotated session alias — idiomatic FastAPI form that satisfies the B008 rule.
SessionDep = Annotated[AsyncSession, Depends(get_session)]

# A modest floor on password length. Not a complexity theater — just a bound.
MIN_PASSWORD_LEN = 8

# A dummy argon2 hash used to keep login timing constant when the email is not
# registered (#29): without it, a missing User short-circuits before the password
# hash check, creating a timing oracle that contradicts the route's own comment.
_DUMMY_HASH = hash_password("constant-time-dummy-do-not-use")


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LEN)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    token: str
    token_type: str = "bearer"


class ResetRequestIn(BaseModel):
    email: EmailStr


class ResetConfirmIn(BaseModel):
    token: str
    password: str = Field(min_length=MIN_PASSWORD_LEN)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=MIN_PASSWORD_LEN)


class VerifyIn(BaseModel):
    token: str


class UserOut(BaseModel):
    id: int
    email: EmailStr
    is_active: bool
    telegram_chat_id: str | None = None


def _client_ip(request: Request) -> str:
    return client_ip_from_request(request)


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(body: SignupIn, request: Request, session: SessionDep) -> dict[str, str]:
    if not await get_ip_limiter().check(_client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts from this IP")
    if not await get_email_limiter().check(body.email.lower()):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts for this email")

    existing = await session.scalar(select(User).where(User.email == body.email.lower()))
    if existing is None:
        user = User(
            email=body.email.lower(), password_hash=hash_password(body.password), is_active=False
        )
        session.add(user)
        await session.flush()
        token, jti = create_purpose_token(user.id, "verify")
        user.verify_token_jti = jti
        await session.commit()
        send_verification_email(user.email, token)
    # Neutral response (#29): identical whether or not the email was already
    # registered, consistent with the reset flow. Returning a session token
    # only for a brand-new account would leak registration status via the body,
    # so the client is directed to verify its email either way.
    return {"detail": "if this email is not already registered, a verification link has been sent"}


@router.post("/verify", response_model=UserOut)
async def verify_email(body: VerifyIn, session: SessionDep) -> UserOut:
    payload = decode_token(body.token, expected_purpose="verify")
    if payload is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired verification link")
    user = await session.get(User, int(payload.sub))
    if user is None or user.verify_token_jti != payload.jti:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "verification link already used or invalid"
        )
    user.is_active = True
    user.verify_token_jti = None  # single-use
    await session.commit()
    return UserOut(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        telegram_chat_id=user.telegram_chat_id,
    )


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, session: SessionDep) -> TokenOut:
    if not await get_ip_limiter().check(_client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts from this IP")
    if not await get_email_limiter().check(body.email.lower()):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts for this email")

    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    # Constant-time password check even for a missing User (#29): run a real
    # argon2 verify against a dummy hash so the response timing does not branch
    # on user existence. The route's old comment claimed this; the code didn't
    # (the `or` short-circuited before the hash check). Evaluate verify_password
    # unconditionally, then decide.
    stored_hash = user.password_hash if user is not None else _DUMMY_HASH
    password_ok = verify_password(body.password, stored_hash)
    if user is None or not password_ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account not verified")
    return TokenOut(token=create_session_token(user.id, session_version=user.session_version))


@router.post("/logout")
async def logout(user: CurrentUser, session: SessionDep) -> dict[str, str]:
    """Invalidate the current session server-side (#30).

    Bumping session_version rejects every previously issued token for this User,
    so logout is not merely client-side cookie deletion — a stolen token dies.
    """
    user.session_version += 1
    await session.commit()
    return {"detail": "logged out"}


@router.post("/reset/request")
async def request_reset(
    body: ResetRequestIn, request: Request, session: SessionDep
) -> dict[str, str]:
    # Enforce the limiter (#29): the result was discarded, so reset requests
    # were unthrottled (each also rotates the reset token, invalidating prior
    # legitimate links). Always 200 — never reveal whether an email is registered.
    if not await get_ip_limiter().check(_client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts from this IP")
    if not await get_email_limiter().check(body.email.lower()):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts for this email")
    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    if user is not None:
        token, jti = create_purpose_token(user.id, "reset")
        user.reset_token_jti = jti
        await session.commit()
        send_reset_email(user.email, token)
    return {"detail": "if the email is registered, a reset link has been sent"}


@router.post("/reset/confirm", response_model=TokenOut)
async def confirm_reset(body: ResetConfirmIn, session: SessionDep) -> TokenOut:
    payload = decode_token(body.token, expected_purpose="reset")
    if payload is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired reset link")
    user = await session.get(User, int(payload.sub))
    if user is None or user.reset_token_jti != payload.jti:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "reset link already used or invalid")
    user.password_hash = hash_password(body.password)
    user.reset_token_jti = None  # single-use
    # Invalidate all prior sessions (#30): a password reset must revoke any
    # stolen token that survived until now.
    user.session_version += 1
    await session.commit()
    return TokenOut(token=create_session_token(user.id, session_version=user.session_version))


@router.post("/password", response_model=UserOut)
async def change_password(
    body: ChangePasswordIn, user: CurrentUser, session: SessionDep
) -> UserOut:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    # Invalidate other sessions (#30). The caller's current token also becomes
    # stale; UI flows re-issue it. API clients must re-authenticate.
    user.session_version += 1
    await session.commit()
    return UserOut(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        telegram_chat_id=user.telegram_chat_id,
    )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        telegram_chat_id=user.telegram_chat_id,
    )
