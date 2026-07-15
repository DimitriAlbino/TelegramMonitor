"""Tests for the production secret guard (#25)."""

from __future__ import annotations

import pytest

from tgmonitor.config import Settings, validate_production_secrets


def _settings(**kw) -> Settings:
    base = dict(
        secret_key="a-real-long-random-secret",
        postgres_password="a-real-password",
    )
    base.update(kw)
    return Settings(**base)


def test_production_rejects_default_secret_key() -> None:
    s = _settings(environment="production", secret_key="change-me-to-a-long-random-string")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        validate_production_secrets(s)


def test_production_rejects_blank_secret_key() -> None:
    s = _settings(environment="production", secret_key="")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        validate_production_secrets(s)


def test_production_rejects_default_postgres_password() -> None:
    s = _settings(environment="production", postgres_password="change-me")
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        validate_production_secrets(s)


def test_production_accepts_real_secrets() -> None:
    s = _settings(environment="production")
    validate_production_secrets(s)  # no raise


def test_development_boots_with_defaults() -> None:
    """Non-production environments still boot with placeholder defaults."""
    s = _settings(
        environment="development",
        secret_key="change-me-to-a-long-random-string",
        postgres_password="change-me",
    )
    validate_production_secrets(s)  # no raise
