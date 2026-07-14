"""TelegramMonitor — hosted monitoring service with Telegram alerts.

Package layout:
- :mod:`tgmonitor.config` — environment-driven settings (single source of truth).
- :mod:`tgmonitor.db` — async SQLAlchemy engine/session.
- :mod:`tgmonitor.models` — ORM (User, Monitor, Check hypertable).
- :mod:`tgmonitor.results` — pure Result type + classification.
- :mod:`tgmonitor.executors` — Check executors (the testable seam).
- :mod:`tgmonitor.api` — FastAPI app.
- :mod:`tgmonitor.worker` — async tick-loop Check engine.
"""

__version__ = "0.1.0"
