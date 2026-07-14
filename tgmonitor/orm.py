"""Declarative base shared by all ORM models.

Kept separate from :mod:`tgmonitor.models` so Alembic's ``env.py`` and tests
can import the metadata without pulling the full model module graph.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base. All models subclass this."""
