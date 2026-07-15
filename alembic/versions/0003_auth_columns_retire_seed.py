"""auth columns + retire the walking-skeleton seed user

Adds email-verification and password-reset columns to ``users``, and retires
the T1 seed user: the walking skeleton's placeholder user
(``demo@telegrammonitor.local``) is replaced by a demo user that owns the seed
Monitor.

NOTE (#24): this migration originally documented a known plaintext password
for the demo account. That account is deactivated and its hash randomized by
migration ``0010_retire_demo_account``; the known credentials no longer work.
The historical hash below is retained only so the migration remains idempotent
against already-applied history — it is not a usable credential on any stack
that has run 0010.

Revision ID: 0003_auth_columns_retire_seed
Revises: 0002_seed_walking_skeleton
Create Date: 2026-07-14 00:02:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_auth_columns_retire_seed"
down_revision: str | None = "0002_seed_walking_skeleton"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The T1 seed created a user at a .local address. That TLD is reserved and the
# API's email validator rejects it, so login would fail. The demo user is
# renamed to a valid domain here, keeping the same id (so the seed Monitor's
# user_id foreign key stays valid).
OLD_SEED_EMAIL = "demo@telegrammonitor.local"
DEMO_EMAIL = "demo@example.com"
# Historical argon2 hash for the seed account. Neutralized by 0010 — kept here
# only so the migration is reproducible against its original revision context.
# Do not treat as a usable credential.
DEMO_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "oLTW+n8vJaSUcs45h5Dy3g$vFUmmojuvPbtGTiFohiWlW9XXzF02tI3PtpGufZpQ0s"
)


def upgrade() -> None:
    # --- New auth columns on users ---
    op.add_column(
        "users",
        sa.Column("verify_token_jti", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("reset_token_jti", sa.String(length=64), nullable=True),
    )
    # is_active now means "email verified" (default False on signup). Existing
    # rows get set to False so they must verify; the demo user is set True below.
    op.alter_column(
        "users",
        "is_active",
        existing_type=sa.Boolean(),
        server_default=sa.text("false"),
    )

    # --- Retire the T1 seed: rename to a valid email + set a real password.
    # The rename keeps the same row id, so the seed Monitor's user_id FK is
    # still valid; the seed Monitor's owner is now a real, loginable User.
    op.execute(
        sa.text(
            "UPDATE users SET email = :new, password_hash = :ph, is_active = true "
            "WHERE email = :old"
        ).bindparams(new=DEMO_EMAIL, ph=DEMO_PASSWORD_HASH, old=OLD_SEED_EMAIL)
    )
    # If the old seed user was deleted somehow, ensure a demo user still exists
    # so the seed Monitor has a valid owner.
    op.execute(
        sa.text(
            "INSERT INTO users (email, password_hash, is_active, created_at) "
            "SELECT :email, :ph, true, now() "
            "WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = :email)"
        ).bindparams(email=DEMO_EMAIL, ph=DEMO_PASSWORD_HASH)
    )


def downgrade() -> None:
    op.alter_column(
        "users", "is_active", existing_type=sa.Boolean(), server_default=sa.text("true")
    )
    op.drop_column("users", "reset_token_jti")
    op.drop_column("users", "verify_token_jti")
