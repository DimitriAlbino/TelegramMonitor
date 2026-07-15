"""telegram chat-link challenge columns

Adds ``telegram_link_code`` and ``telegram_link_expires_at`` to ``users`` for
the verified chat-linking flow (#26): a user requests a one-time code, sends
``/link <code>`` from the target chat, and the webhook binds that chat's id to
the user only if the code matches. This proves the caller controls the chat
before pointing alerts/reports at it, closing the alert-hijack path where any
caller could set any chat_id.

Also adds a unique index on ``telegram_chat_id`` so two accounts cannot silently
claim the same chat (a conflict is surfaced explicitly rather than splitting
delivery).

Revision ID: 0011_telegram_link_challenge
Revises: 0010_retire_demo_account
Create Date: 2026-07-15 00:10:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_telegram_link_challenge"
down_revision: str | None = "0010_retire_demo_account"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("telegram_link_code", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("telegram_link_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # A partial unique index: only enforce uniqueness for actually-linked chats.
    # Two unlinked users have NULL chat_id; we must not block multiple NULLs.
    op.execute(
        "CREATE UNIQUE INDEX ix_users_telegram_chat_id_unique "
        "ON users (telegram_chat_id) WHERE telegram_chat_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_telegram_chat_id_unique")
    op.drop_column("users", "telegram_link_expires_at")
    op.drop_column("users", "telegram_link_code")
