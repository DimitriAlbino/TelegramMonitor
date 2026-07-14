"""continuous aggregates (hourly/daily rollups) + scheduled reports + post-incident summary

Sets up TimescaleDB continuous aggregates for cheap uptime/latency reporting
(ADR-0006/0008): an hourly rollup (retained 1 year) and a daily rollup (retained
forever), both derived from the raw checks hypertable. Adds the reports table
(scheduled-report jobs, DB-driven next_run_at like checks) and a post-incident
summary column.

Revision ID: 0007_rollups_reports_postsummary
Revises: 0006_quiet_hours
Create Date: 2026-07-14 00:06:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_rollups_reports_postsummary"
down_revision: str | None = "0006_quiet_hours"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Hourly rollup continuous aggregate ---
    # Per Monitor per hour: total checks, successes (for uptime %), and latency
    # aggregates. TimescaleDB materializes this incrementally.
    op.execute(
        """
        CREATE MATERIALIZED VIEW checks_hourly
        WITH (timescaledb.continuous) AS
        SELECT
            time_bucket('1 hour', checked_at) AS bucket,
            monitor_id,
            count(*)                          AS total,
            count(*) FILTER (WHERE success)   AS successes,
            count(*) FILTER (WHERE NOT success) AS failures,
            max(latency_ms)                   AS max_latency_ms,
            avg(latency_ms)                   AS avg_latency_ms
        FROM checks
        GROUP BY bucket, monitor_id
        WITH NO DATA
        """
    )
    # Keep the aggregate fresh. The refresh window must cover at least two
    # buckets; use a generous start_offset so this works on an empty hypertable.
    op.execute(
        "SELECT add_continuous_aggregate_policy('checks_hourly', "
        "start_offset => INTERVAL '7 days', end_offset => INTERVAL '1 hour', "
        "schedule_interval => INTERVAL '1 hour', if_not_exists => true)"
    )
    # Retain hourly rollups for 1 year (ADR-0008). TimescaleDB applies
    # retention to the materialization view directly; the policy survives
    # version differences by referencing the view name.
    op.execute(
        "SELECT add_retention_policy('checks_hourly', INTERVAL '1 year', if_not_exists => true)"
    )

    # --- Daily rollup continuous aggregate (retained forever) ---
    op.execute(
        """
        CREATE MATERIALIZED VIEW checks_daily
        WITH (timescaledb.continuous) AS
        SELECT
            time_bucket('1 day', checked_at) AS bucket,
            monitor_id,
            count(*)                          AS total,
            count(*) FILTER (WHERE success)   AS successes,
            count(*) FILTER (WHERE NOT success) AS failures,
            max(latency_ms)                   AS max_latency_ms,
            avg(latency_ms)                   AS avg_latency_ms
        FROM checks
        GROUP BY bucket, monitor_id
        WITH NO DATA
        """
    )
    op.execute(
        "SELECT add_continuous_aggregate_policy('checks_daily', "
        "start_offset => INTERVAL '30 days', end_offset => INTERVAL '1 day', "
        "schedule_interval => INTERVAL '1 day', if_not_exists => true)"
    )
    # Daily rollup retained forever (ADR-0008): no retention policy.

    # --- Reports table (scheduled-report jobs, DB-driven next_run_at) ---
    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("monitor_id", sa.BigInteger(), nullable=True),
        # daily | weekly | monthly
        sa.Column("cadence", sa.String(length=16), nullable=False),
        # HH:MM delivery time in the user's tz
        sa.Column("delivery_time", sa.String(length=5), nullable=False, server_default="08:00"),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        # Rendered body kept 30d then GC'd (ADR-0008); metadata kept forever.
        sa.Column("last_body", sa.Text(), nullable=True),
        sa.Column("last_rendered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reports_next_run_at", "reports", ["next_run_at"])
    op.create_index("ix_reports_user_id", "reports", ["user_id"])

    # --- Post-incident summary column on incidents ---
    # Set when the post-incident summary is sent, so we don't resend.
    op.add_column(
        "incidents",
        sa.Column("summary_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    # failed_check_count is already on incidents (migration 0004) — populated on
    # close for the summary.


def downgrade() -> None:
    op.drop_column("incidents", "summary_sent_at")
    op.drop_index("ix_reports_user_id", table_name="reports")
    op.drop_index("ix_reports_next_run_at", table_name="reports")
    op.drop_table("reports")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS checks_daily")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS checks_hourly")
