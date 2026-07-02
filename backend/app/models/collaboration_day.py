"""SQLAlchemy ORM model for scheduled collaboration days.

Each row is a single day on which an employee is scheduled to collaborate
on-site (a workspace / room booking). ``employee_name`` must match
:class:`~app.models.employee.Employee`.``name`` so the reminder job can resolve
an owner key. Reuses :class:`TimestampStatusMixin` for the common PostgreSQL
fields.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampStatusMixin


class CollaborationDay(Base, TimestampStatusMixin):
    """A single scheduled on-site collaboration day for an employee."""

    __tablename__ = "collaboration_days"

    employee_name: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    weekday: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ws: Mapped[str | None] = mapped_column(String(32), nullable=True)
    room: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # Serves per-employee, per-date lookups for the reminder job.
        Index("ix_collaboration_days_name_date", "employee_name", "date"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CollaborationDay(id={self.id!r}, employee_name={self.employee_name!r}, "
            f"date={self.date!r})"
        )
