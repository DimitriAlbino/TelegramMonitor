"""session revocation version on users

Adds a ``session_version`` integer column to ``users`` (default 0). Each session
JWT embeds the value at issue time; the auth dependencies compare it to this
column on every protected request. Bumping the column (logout / password change
/ password reset) invalidates every previously issued session for that User, so
a stolen long-lived token cannot survive an account recovery (#30).

Revision ID: 0009_session_revocation
Revises: 0008_status_page
Create Date: 2026-07-15 00:08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_session_revocation"
down_revision: str | None = "0008_status_page"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_version", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "session_version")
