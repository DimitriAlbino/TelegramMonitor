"""monitor.incident_status column

Adds the authoritative Incident state-machine status (ok/down/flapping) to the
monitors table so the SM state is reconstructed exactly on each tick — not
inferred from counters (which led to a recovery bug where a down monitor's
accumulating successes were wiped).

Revision ID: 0005_monitor_incident_status
Revises: 0004_incidents_table
Create Date: 2026-07-14 00:04:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_monitor_incident_status"
down_revision: str | None = "0004_incidents_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "monitors",
        sa.Column("incident_status", sa.String(length=16), server_default="ok", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("monitors", "incident_status")
