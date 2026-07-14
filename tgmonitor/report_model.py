"""The Report ORM model — a scheduled-report job (ADR-0006 kind #1).

A Report carries a cadence (daily/weekly/monthly) and a delivery time. The
worker's scheduler reuses the same DB-driven next_run_at pattern as Checks: a
tick scans for due reports, renders the digest, delivers it, advances next_run_at.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from tgmonitor.models import UTC_TIMESTAMP, utcnow
from tgmonitor.orm import Base


class Report(Base):
    """A scheduled periodical Report job."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL means "all the user's monitors"; a specific monitor_id scopes it.
    monitor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cadence: Mapped[str] = mapped_column(String(16), nullable=False)  # daily|weekly|monthly
    delivery_time: Mapped[str] = mapped_column(String(5), nullable=False, default="08:00")
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP, default=utcnow, server_default=text("now()"), index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, nullable=True)
    # Rendered body kept 30d then GC'd (ADR-0008); the row (metadata) is kept forever.
    last_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_rendered_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP, default=utcnow, server_default=text("now()")
    )

    def __repr__(self) -> str:
        return f"<Report id={self.id} user_id={self.user_id} cadence={self.cadence}>"
