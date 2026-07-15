"""retire the seeded demo account (known credentials)

Migration 0003 created/renamed ``demo@example.com`` with a committed argon2
hash for ``***REMOVED***`` and ``is_active = true``; migrations run on worker
startup, so every deployed stack has this active account unless manually
deleted. The plaintext was documented in the migration docstring (#24).

This migration deactivates the demo account and replaces its known password
hash with a random value, so the documented credentials no longer work on any
stack that upgrades to it. We do not rewrite the already-shipped 0002/0003
migrations in place (they may have been applied); we neutralize the account
going-forward. The seed monitor becomes unowned-orphaned visually but is left
in place (it is owned by the now-inactive user).

Revision ID: 0010_retire_demo_account
Revises: 0009_session_revocation
Create Date: 2026-07-15 00:09:00

"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_retire_demo_account"
down_revision: str | None = "0009_session_revocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEMO_EMAIL = "demo@example.com"


def upgrade() -> None:
    # Deactivate the demo account and overwrite the known password hash with a
    # random token so the documented credentials cannot log in. A bump of
    # session_version also invalidates any session forged with the old hash.
    random_hash = secrets.token_urlsafe(48)
    op.execute(
        sa.text(
            "UPDATE users SET is_active = false, "
            "password_hash = :ph, session_version = session_version + 1 "
            "WHERE email = :email"
        ).bindparams(ph=random_hash, email=DEMO_EMAIL)
    )


def downgrade() -> None:
    # Re-activating the known-credential demo account would reintroduce the
    # vulnerability; downgrade is a no-op by design.
    pass
