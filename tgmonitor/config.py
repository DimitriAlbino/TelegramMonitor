"""Environment-driven configuration.

One :class:`Settings` instance, hydrated from environment variables (or ``.env``).
Every variable read by any service is declared here so this module is the single
source of truth — the ``.env.example`` file mirrors these names one-for-one.

See ADR-0003 (stack), ADR-0004 (engine knobs).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core ---
    secret_key: str = Field(default="change-me-to-a-long-random-string")
    environment: str = Field(default="development")

    # --- Database ---
    postgres_user: str = Field(default="tgmonitor")
    postgres_password: str = Field(default="change-me")
    postgres_db: str = Field(default="tgmonitor")
    # Documented form is the sync SQLAlchemy URL (postgresql+psycopg://).
    # Async and raw psycopg URLs are derived below so only one env var is needed.
    database_url: str = Field(default="postgresql+psycopg://tgmonitor:change-me@db:5432/tgmonitor")

    # --- Telegram (unused by T1; declared so .env.example stays the contract) ---
    telegram_bot_token: str = Field(default="")
    telegram_webhook_public_url: str = Field(default="")
    telegram_webhook_secret: str = Field(default="")
    telegram_login_bot_name: str = Field(default="")
    telegram_login_provider_token: str = Field(default="")

    # --- Email (unused by T1) ---
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from: str = Field(default="noreply@monitor.example.com")

    # --- Engine knobs (ADR-0004, defaults from .env.example) ---
    check_tick_interval_s: float = Field(default=5.0)
    check_concurrency: int = Field(default=200)
    min_check_interval_s: int = Field(default=30)
    max_monitors_per_user: int = Field(default=50)

    # --- Logging ---
    log_level: str = Field(default="INFO")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_database_url(self) -> str:
        """Async SQLAlchemy URL (psycopg3 async).

        Converts ``postgresql+psycopg://...`` → ``postgresql+psycopg_async://...``.
        Other dialects pass through unchanged.
        """
        if self.database_url.startswith("postgresql+psycopg://"):
            return self.database_url.replace(
                "postgresql+psycopg://", "postgresql+psycopg_async://", 1
            )
        return self.database_url

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """Sync SQLAlchemy URL (psycopg3 sync) for Alembic.

        Normalizes to ``postgresql+psycopg://`` so SQLAlchemy picks the psycopg3
        sync dialect explicitly. A bare ``postgresql://`` would resolve to the
        legacy psycopg2 dialect, which is not installed in this project.
        """
        if self.database_url.startswith("postgresql+psycopg_async://"):
            return self.database_url.replace(
                "postgresql+psycopg_async://", "postgresql+psycopg://", 1
            )
        return self.database_url

    @computed_field  # type: ignore[prop-decorator]
    @property
    def raw_psycopg_dsn(self) -> str:
        """Plain ``postgresql://`` DSN for raw psycopg3 (libpq) calls.

        ``psycopg.connect()`` wants a libpq DSN, not a SQLAlchemy URL, so the
        ``+psycopg`` driver suffix must be stripped. Used by the worker's
        DB-readiness check.
        """
        for prefix in ("postgresql+psycopg_async://", "postgresql+psycopg://"):
            if self.database_url.startswith(prefix):
                return self.database_url.replace(prefix, "postgresql://", 1)
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton — import once, read everywhere."""
    return Settings()
