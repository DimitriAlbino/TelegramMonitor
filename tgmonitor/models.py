"""ORM models — User, Monitor, and the Check-result hypertable.

Vocabulary is normative (CONTEXT.md): table and column names use the canonical
terms (Monitor, Check, Result fields) and avoid the glossary's _Avoid_ words.

The ``checks`` table is a TimescaleDB hypertable partitioned by ``checked_at``;
the migration calls ``create_hypertable`` (ADR-0008). T1 sets up the raw
hypertable + 30-day retention policy. Continuous aggregates (hourly/daily
rollups) arrive with the Reports tickets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tgmonitor.orm import Base

# A tz-aware UTC timestamp with microsecond precision and (timezone.utc) at the
# SQL level. We store everything in UTC.
UTC_TIMESTAMP: Any = TIMESTAMP(timezone=True)


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    """A person with an account who owns Monitors and receives notifications.

    CONTEXT.md: "A person with an account on the service who owns monitors and
    receives notifications."
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    # Hashed with argon2 (passlib). Never stored or logged in plaintext.
    password_hash: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # Telegram chat binding — populated by the account-linking flow (T4). Null
    # until linked. The Notification Channel resolves to this.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Email verification: an account is inactive until the verification link is
    # clicked. ``is_active`` flips to true on verification (ADR-0001).
    is_active: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # A signed one-time token carrying the verification intent; the JWT itself
    # encodes the user id + purpose, so no DB column is needed for it. Reset
    # works the same way. We track the *latest* issued token jti to invalidate
    # stale links: a verification/reset is single-use.
    verify_token_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reset_token_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Session-revocation version (#30). Embedded in each session JWT; the auth
    # dependencies compare the token's version to this column. Bumping it (on
    # logout / password change / password reset) invalidates every previously
    # issued session for this User, so a stolen 14-day token cannot survive an
    # account recovery.
    session_version: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # Per-User quiet hours (ADR-0005): "HH:MM" start/end in the user's local
    # time, or NULL if disabled. Non-critical Alerts during the window are
    # deferred into a digest at window end; critical Monitors bypass.
    quiet_hours_start: Mapped[str | None] = mapped_column(String(5), nullable=True)
    quiet_hours_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    quiet_hours_tz: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP, default=utcnow, server_default=text("now()")
    )

    monitors: Mapped[list[Monitor]] = relationship(back_populates="user")

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"


class Monitor(Base):
    """A user-configured thing to watch, plus how/when to probe it.

    CONTEXT.md: "A user-configured thing to watch — a URL or a host:port —
    together with how often to probe it and what counts as failure. A Monitor
    is a configuration entity; it does not run continuously by itself."

    Two independent state flags (ADR-0007):
    - ``paused`` (web-UI-settable): the engine stops running Checks.
    - ``muted``  (Telegram-settable): suppresses Alert delivery only.

    ``check_kind`` is the Check Kind (CONTEXT.md): "http", "tcp", or "api_content". The
    kind-specific config (expected status, keyword, max latency, host/port,
    timeout) lives in the JSONB ``config`` column so adding a kind is additive.
    """

    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # http | tcp. Adding a kind is additive; validated at the API layer.
    check_kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # The probe target: a URL (http) or host:port (tcp).
    target: Mapped[str] = mapped_column(String(2048), nullable=False)
    # Kind-specific knobs. For http: {expected_status, body_contains,
    # max_latency_ms, timeout_s}. For tcp: {timeout_s}.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")

    interval_s: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Engine scheduling cursor (ADR-0004). The worker claims monitors whose
    # next_check_at <= now, runs the Check, advances next_check_at.
    next_check_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP, default=utcnow, server_default=text("now()"), index=True
    )

    # Incident state-machine cursor (ADR-0005). Counts and status are maintained
    # by the state machine in T5. The authoritative status (ok/down/flapping) is
    # persisted here so reconstruction on the next tick is exact, not heuristic.
    failure_threshold: Mapped[int] = mapped_column(default=3, server_default=text("3"))
    recovery_threshold: Mapped[int] = mapped_column(default=2, server_default=text("2"))
    consecutive_failures: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    consecutive_successes: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # ok | down | flapping (the Incident SM status; CONTEXT.md / ADR-0005).
    incident_status: Mapped[str] = mapped_column(
        String(16), default="ok", server_default=text("'ok'")
    )

    # Two independent flags. A Monitor can be neither, either, or both.
    paused: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    muted: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # Bypasses quiet hours when True (ADR-0005).
    critical: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # Opt a Monitor into the public Status Page (ADR-0009).
    show_on_status_page: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    created_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP, default=utcnow, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP, default=utcnow, onupdate=utcnow, server_default=text("now()")
    )

    user: Mapped[User] = relationship(back_populates="monitors")

    __table_args__ = (Index("ix_monitors_next_check_due", "next_check_at", "paused"),)

    def __repr__(self) -> str:
        return f"<Monitor id={self.id} name={self.name!r} kind={self.check_kind}>"


class Check(Base):
    """A single Check execution's Result, stored in a TimescaleDB hypertable.

    CONTEXT.md:
        Check — A single execution of a Monitor's probe at a point in time,
        producing a Result. "A check ran at 10:03."
        Result — The outcome of one Check: success/failure, latency, status
        code, and a human-readable reason. Stored as an append-only time series
        per Monitor.

    This table is the append-only time series. It is partitioned by
    ``checked_at`` (``create_hypertable`` in the migration). Retention: raw 30d
    via ``add_retention_policy`` (ADR-0008).
    """

    __tablename__ = "checks"

    # TimescaleDB requires every unique index (including the PK) to include the
    # partitioning column (checked_at). So the PK is the composite
    # (monitor_id, checked_at) — also the natural identity of a Result (which
    # monitor, when). There is no separate surrogate id; the hypertable is the
    # append-only time series and rows are addressed by (monitor, time).
    monitor_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("monitors.id", ondelete="CASCADE"),
        primary_key=True,
    )
    checked_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP,
        nullable=False,
        primary_key=True,
        default=utcnow,
        server_default=text("now()"),
    )
    success: Mapped[bool] = mapped_column(nullable=False)
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)

    # The PK (monitor_id, checked_at) already serves the monitor + time lookup;
    # no separate index needed — the hypertable partition key covers it.

    def __repr__(self) -> str:
        return (
            f"<Check monitor_id={self.monitor_id} checked_at={self.checked_at} "
            f"success={self.success}>"
        )
