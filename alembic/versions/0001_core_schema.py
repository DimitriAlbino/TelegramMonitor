"""core schema: users, monitors, checks hypertable + timescaledb

Creates the three core tables and converts the high-volume checks table into a
TimescaleDB hypertable partitioned by checked_at, with a 30-day raw retention
policy (ADR-0008). Continuous aggregates (hourly/daily rollups) are deferred to
the Reports tickets — T1 only stands up the raw stream + retention primitive.

Revision ID: 0001_core_schema
Revises:
Create Date: 2026-07-14 00:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_core_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- TimescaleDB extension (required before hypertables) ---
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), server_default="", nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # --- monitors ---
    op.create_table(
        "monitors",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("check_kind", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=2048), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("interval_s", sa.BigInteger(), nullable=False),
        sa.Column(
            "next_check_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("failure_threshold", sa.Integer(), server_default="3", nullable=False),
        sa.Column("recovery_threshold", sa.Integer(), server_default="2", nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consecutive_successes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("muted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("critical", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "show_on_status_page",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_monitors_user_id", "monitors", ["user_id"])
    op.create_index("ix_monitors_check_kind", "monitors", ["check_kind"])
    # Composite index serving the worker's due-query: WHERE paused=false
    # AND next_check_at <= now() ORDER BY next_check_at.
    op.create_index("ix_monitors_next_check_due", "monitors", ["next_check_at", "paused"])

    # --- checks (the append-only time series) ---
    # Composite PK (monitor_id, checked_at): TimescaleDB requires the
    # partitioning column (checked_at) to be in every unique index, including
    # the PK. This composite is also the natural identity of a Result.
    op.create_table(
        "checks",
        sa.Column("monitor_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.ForeignKeyConstraint(["monitor_id"], ["monitors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("monitor_id", "checked_at"),
    )

    # --- Convert checks into a TimescaleDB hypertable partitioned by checked_at.
    # ``migrate_data => true`` is harmless on an empty table; ``chunk_time_interval``
    # of 7 days suits the raw probe volume at launch.
    op.execute(
        "SELECT create_hypertable('checks', 'checked_at', "
        "chunk_time_interval => INTERVAL '7 days', migrate_data => true, "
        "if_not_exists => true)"
    )

    # --- 30-day raw retention policy (ADR-0008). ---
    # add_retention_policy schedules an automated drop_chunks job. The integer
    # retention interval form (drop_after => INTERVAL '30 days') is supported on
    # TimescaleDB >= 2.0.
    op.execute("SELECT add_retention_policy('checks', INTERVAL '30 days', if_not_exists => true)")


def downgrade() -> None:
    op.drop_table("checks")
    op.drop_index("ix_monitors_next_check_due", table_name="monitors")
    op.drop_index("ix_monitors_check_kind", table_name="monitors")
    op.drop_index("ix_monitors_user_id", table_name="monitors")
    op.drop_table("monitors")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    # Leave the extension in place; other DBs may use it.
