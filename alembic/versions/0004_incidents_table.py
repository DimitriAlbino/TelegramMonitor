"""incidents table

Adds the Incident table (ADR-0005/0008). Incidents are low-volume transition
records retained forever: an open Incident has closed_at IS NULL; closing sets
closed_at, close_reason, and outcome.

Revision ID: 0004_incidents_table
Revises: 0003_auth_columns_retire_seed
Create Date: 2026-07-14 00:03:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_incidents_table"
down_revision: str | None = "0003_auth_columns_retire_seed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("monitor_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open_reason", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("close_reason", sa.String(length=512), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("failed_check_count", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["monitor_id"], ["monitors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incidents_monitor_id", "incidents", ["monitor_id"])
    op.create_index("ix_incidents_monitor_open", "incidents", ["monitor_id", "opened_at"])


def downgrade() -> None:
    op.drop_index("ix_incidents_monitor_open", table_name="incidents")
    op.drop_index("ix_incidents_monitor_id", table_name="incidents")
    op.drop_table("incidents")
