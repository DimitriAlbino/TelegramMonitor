"""status page table

Adds the StatusPage entity (ADR-0009): one optional public page per User at a
stable URL slug, with a title/subtitle. The page reflects the live, auto-
generated state of Monitors with show_on_status_page=true.

Revision ID: 0008_status_page
Revises: 0007_rollups_reports_postsummary
Create Date: 2026-07-14 00:07:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_status_page"
down_revision: str | None = "0007_rollups_reports_postsummary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "status_pages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        # Stable URL slug, unique across the service.
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False, server_default="Status"),
        sa.Column("subtitle", sa.String(length=512), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_status_pages_user_id", "status_pages", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_status_pages_user_id", table_name="status_pages")
    op.drop_table("status_pages")
