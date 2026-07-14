"""The Check engine — a single async worker with a DB-driven tick loop.

ADR-0004: one long-running asyncio process. Every ``CHECK_TICK_INTERVAL_S``
(default 5s) it queries Monitors with ``next_check_at <= now``, claims each due
monitor atomically (advancing ``next_check_at`` in the same statement), and
runs the Check under a global semaphore. State is in the DB, so restarts and
deploys resume cleanly with no in-memory reconciliation.

Concurrency is bounded by ``CHECK_CONCURRENCY`` (default 200) so a burst of due
monitors cannot exhaust sockets or memory.
"""

from __future__ import annotations

from .engine import CheckEngine

__all__ = ["CheckEngine"]
