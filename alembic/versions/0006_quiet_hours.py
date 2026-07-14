"""quiet hours columns on users

Adds per-User quiet hours (ADR-0005): a start/end time in the user's timezone
during which non-critical Alert delivery is deferred into a digest at window
end. Critical Monitors bypass quiet hours.

Revision ID: 0006_quiet_hours
Revises: 0005_monitor_incident_status
Create Date: 2026-07-14 00:05:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_quiet_hours"
down_revision: str | None = "0005_monitor_incident_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # quiet_hours_start / quiet_hours_end: "HH:MM" in the user's local time, or
    # NULL if quiet hours are disabled. quiet_hours_tz: IANA timezone name.
    op.add_column(
        "users",
        sa.Column("quiet_hours_start", sa.String(length=5), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("quiet_hours_end", sa.String(length=5), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("quiet_hours_tz", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "quiet_hours_tz")
    op.drop_column("users", "quiet_hours_end")
    op.drop_column("users", "quiet_hours_start")
