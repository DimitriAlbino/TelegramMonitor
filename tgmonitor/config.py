"""Environment-driven configuration.

One :class:`Settings` instance, hydrated from environment variables (or ``.env``).
Every variable read by any service is declared here so this module is the single
source of truth; ``.env.example`` documents the same set.

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

    # --- Telegram (shared bot) ---
    # From @BotFather; the bot token used for outbound alerts and the inbound
    # webhook. Empty in dev (sends are logged and skipped).
    telegram_bot_token: str = Field(default="")
    # Secret token we set when registering the webhook; Telegram echoes it back
    # in the X-Telegram-Bot-Api-Secret-Token header (ADR-0012).
    telegram_webhook_secret: str = Field(default="")

    # --- Email (SMTP for signup verification + password reset) ---
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from: str = Field(default="noreply@monitor.example.com")
    # Friendly sender name shown in the recipient's inbox (e.g. "TelegramMonitor").
    smtp_from_name: str = Field(default="TelegramMonitor")

    # --- Engine knobs (ADR-0004, defaults from .env.example) ---
    check_tick_interval_s: float = Field(default=5.0)
    check_concurrency: int = Field(default=200)
    min_check_interval_s: int = Field(default=30)
    max_monitors_per_user: int = Field(default=50)

    # --- Logging ---
    log_level: str = Field(default="INFO")

    # --- Auth (ADR-0001) ---
    # JWT signing key — must be a long random string in production.
    # Sessions expire after this many minutes (default ~14 days).
    session_expire_minutes: int = Field(default=60 * 24 * 14)
    # Rate limiting (abuse prevention, ADR-0011): max auth attempts per window.
    auth_rate_window_s: int = Field(default=300)
    auth_rate_max_per_ip: int = Field(default=20)
    auth_rate_max_per_email: int = Field(default=10)
    # Trust X-Forwarded-For from the reverse proxy for client-IP rate limiting
    # (#29). Only enable when the deploy genuinely sits behind a proxy we
    # control (Caddy), so the header cannot be spoofed by arbitrary clients.
    trust_proxy_headers: bool = Field(default=False)
    # Where the web UI lives, for building verification/reset links.
    public_base_url: str = Field(default="http://localhost:8000")

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


# Sentinel default values that must never reach a production deploy (#25).
DEFAULT_SECRET_KEY = "change-me-to-a-long-random-string"
DEFAULT_POSTGRES_PASSWORD = "change-me"


def validate_production_secrets(settings: Settings) -> None:
    """Fail fast if security-critical secrets are still default in production (#25).

    ``secret_key`` keys the stateless HS256 session JWTs: a deploy that forgets
    to set it lets anyone forge a session for any user id. ``POSTGRES_PASSWORD``
    similarly defaults to ``change-me``. When ``ENVIRONMENT=production``, leaving
    either at its placeholder default (or blank) is a hard startup error. Non-
    production environments still boot with defaults for convenience.
    """
    if settings.environment != "production":
        return
    problems: list[str] = []
    if not settings.secret_key or settings.secret_key == DEFAULT_SECRET_KEY:
        problems.append("SECRET_KEY is unset or still the placeholder default")
    if not settings.postgres_password or settings.postgres_password == DEFAULT_POSTGRES_PASSWORD:
        problems.append("POSTGRES_PASSWORD is unset or still the placeholder default")
    if problems:
        raise RuntimeError(
            "Refusing to start in production with insecure defaults: "
            + "; ".join(problems)
            + ". Set real values in the environment / .env."
        )


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton — import once, read everywhere."""
    return Settings()
