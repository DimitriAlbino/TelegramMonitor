"""Alembic environment.

URL comes from :class:`tgmonitor.config.Settings` (DATABASE_URL) so the same
config drives the app and migrations. Both online and offline modes are
supported; offline (`alembic upgrade head --sql`) emits SQL without a DB, used
in CI to catch migration syntax errors.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the project importable when alembic is invoked from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tgmonitor import models  # noqa: F401  (register models on Base.metadata)
from tgmonitor.config import get_settings
from tgmonitor.orm import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Single source of truth for the DB URL. Use the explicit psycopg3 sync dialect
# so SQLAlchemy does not fall back to the legacy psycopg2 driver.
config.set_main_option("sqlalchemy.url", get_settings().sync_database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (CI/``--sql``)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
