"""SQLAlchemy ORM model for the reminder dedup ledger.

Records that a collaboration-day reminder was sent to an ``owner`` for a given
``collab_date``. The ``(owner, collab_date)`` UNIQUE constraint makes the ledger
idempotent so the job never double-notifies for the same day. Reuses
:class:`TimestampStatusMixin` for the common PostgreSQL fields.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampStatusMixin


class ReminderSent(Base, TimestampStatusMixin):
    """A dedup-ledger row: one reminder sent to an owner for a collab date."""

    __tablename__ = "reminders_sent"

    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    collab_date: Mapped[date] = mapped_column(Date, nullable=False)
    channel: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "owner", "collab_date", name="uq_reminders_sent_owner_date"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ReminderSent(id={self.id!r}, owner={self.owner!r}, "
            f"collab_date={self.collab_date!r})"
        )
