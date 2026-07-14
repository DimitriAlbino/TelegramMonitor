"""Entrypoint: ``python -m tgmonitor.worker`` runs the Check engine.

Waits for the DB to be reachable (the Compose ``depends_on`` healthcheck covers
the common case, but this also tolerates the worker starting first), applies
migrations so a fresh stack is immediately runnable, then runs the engine.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from tgmonitor.config import get_settings


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _wait_for_db(timeout_s: float = 60.0) -> None:
    """Block until the DB accepts connections, or time out.

    The Compose healthcheck handles ordering in the happy path; this is the
    belt-and-braces so the worker doesn't crash-loop if it starts first.
    """
    import time

    import psycopg

    settings = get_settings()
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(settings.raw_psycopg_dsn, connect_timeout=3):
                return
        except Exception as exc:
            last_err = exc
            time.sleep(1.0)
    log = logging.getLogger("tgmonitor.worker")
    log.error("could not connect to DB within %.0fs: %s", timeout_s, last_err)
    sys.exit(1)


def _apply_migrations() -> None:
    """Apply Alembic migrations to head so a fresh stack is immediately runnable.

    Uses the sync psycopg URL (Alembic is sync). Runs in the worker because it
    is the first service that needs the schema; the API would also work but
    running migrations here keeps the responsibility in one place.
    """
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


async def _main() -> None:
    from tgmonitor.worker.engine import CheckEngine

    engine = CheckEngine()
    await engine.run()


def main() -> None:
    _configure_logging()
    log = logging.getLogger("tgmonitor.worker")
    log.info("worker starting")
    _wait_for_db()
    _apply_migrations()
    log.info("migrations applied; entering tick loop")
    asyncio.run(_main())


if __name__ == "__main__":
    main()
