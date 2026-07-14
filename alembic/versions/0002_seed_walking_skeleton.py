"""seed walking skeleton: one user + one http monitor (example.com)

Inserts a demo User (no auth yet — T2 retires the seed) and an HTTP Monitor
probing https://example.com every 30s with a 200 expectation. This makes the
walking skeleton demoable out of the box: ``docker compose up`` and the worker
immediately has something to probe, queryable via the results endpoint.

Idempotent at the SQL level (WHERE NOT EXISTS) so it is safe to re-run, and so
it generates correct offline SQL via ``alembic upgrade head --sql``.

Revision ID: 0002_seed_walking_skeleton
Revises: 0001_core_schema
Create Date: 2026-07-14 00:01:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_seed_walking_skeleton"
down_revision: str | None = "0001_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_EMAIL = "demo@telegrammonitor.local"
SEED_MONITOR_TARGET = "https://example.com"
SEED_MONITOR_NAME = "example.com (seed)"

# Idempotent insert of the seed user. The WHERE NOT EXISTS guard makes the
# statement a no-op on re-run and keeps offline SQL generation valid.
INSERT_SEED_USER = sa.text(
    """
    INSERT INTO users (email, password_hash, is_active, created_at)
    SELECT :email, '', true, now()
    WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = :email)
    """
)

# Idempotent insert of the seed HTTP Monitor. The user reference is resolved by
# email in the subquery so the statement does not depend on a Python-captured
# id — this is what makes it work under offline SQL generation.
INSERT_SEED_MONITOR = sa.text(
    """
    INSERT INTO monitors (
        user_id, name, check_kind, target, config, interval_s, next_check_at,
        failure_threshold, recovery_threshold, consecutive_failures,
        consecutive_successes, paused, muted, critical, show_on_status_page,
        created_at, updated_at
    )
    SELECT u.id, :name, 'http', :target,
           jsonb_build_object('expected_status', 200, 'timeout_s', 10),
           :interval_s, now(), 3, 2, 0, 0, false, false, false, false, now(), now()
    FROM users u
    WHERE u.email = :email
      AND NOT EXISTS (
          SELECT 1 FROM monitors m WHERE m.user_id = u.id AND m.target = :target
      )
    """
)


def upgrade() -> None:
    op.execute(
        INSERT_SEED_USER.bindparams(
            email=SEED_EMAIL,
        )
    )
    op.execute(
        INSERT_SEED_MONITOR.bindparams(
            email=SEED_EMAIL,
            name=SEED_MONITOR_NAME,
            target=SEED_MONITOR_TARGET,
            interval_s=30,
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM monitors WHERE target = :target").bindparams(
            target=SEED_MONITOR_TARGET
        )
    )
    op.execute(sa.text("DELETE FROM users WHERE email = :email").bindparams(email=SEED_EMAIL))
