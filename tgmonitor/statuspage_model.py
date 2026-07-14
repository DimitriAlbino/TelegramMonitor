"""The StatusPage ORM model (ADR-0009).

One optional public page per User at a stable URL slug. The page reflects the
live, auto-generated state of Monitors with ``show_on_status_page=true``.
Read-only, unauthenticated, heavily cached (~60s).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, text
from sqlalchemy.orm import Mapped, mapped_column

from tgmonitor.models import UTC_TIMESTAMP, utcnow
from tgmonitor.orm import Base


class StatusPage(Base):
    """One optional public status page per User."""

    __tablename__ = "status_pages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="Status")
    subtitle: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP, default=utcnow, server_default=text("now()")
    )

    def __repr__(self) -> str:
        return f"<StatusPage id={self.id} slug={self.slug!r}>"
