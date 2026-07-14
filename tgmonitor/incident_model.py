"""The Incident ORM model.

CONTEXT.md:
    Incident — A contiguous span during which a Monitor was in a failing state,
    opened when failures cross a threshold and closed on recovery. The unit an
    alert fires about.

Incidents are low-volume (only transitions) and human-meaningful, so they are
retained forever (ADR-0008) in a plain table. An open Incident has
``closed_at IS NULL``; closing sets ``closed_at`` and ``recovery_reason``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tgmonitor.models import UTC_TIMESTAMP, utcnow
from tgmonitor.orm import Base


class Incident(Base):
    """A contiguous failing span for a Monitor, opened/closed by the state machine."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    monitor_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    opened_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP, nullable=False, default=utcnow, server_default=text("now()")
    )
    closed_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, nullable=True)
    # The reason from the Check Result that triggered the open.
    open_reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # The reason from the Check Result that triggered the close (recovery).
    close_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Final disposition: "recovered" | "flapping" | null (still open).
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Counts/summary filled on close for the post-incident summary (T7).
    failed_check_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default="{}"
    )

    __table_args__ = (Index("ix_incidents_monitor_open", "monitor_id", "opened_at"),)

    def __repr__(self) -> str:
        return (
            f"<Incident id={self.id} monitor_id={self.monitor_id} "
            f"opened_at={self.opened_at} closed_at={self.closed_at}>"
        )
