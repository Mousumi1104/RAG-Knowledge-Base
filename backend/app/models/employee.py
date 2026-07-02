"""SQLAlchemy ORM model for the employee directory.

A minimal directory that maps a human ``name`` (which must match
:class:`~app.models.collaboration_day.CollaborationDay`.``employee_name``) to an
``employee_id`` owner key and an optional ``email``. Ships UNSEEDED. Reuses
:class:`TimestampStatusMixin` for the common PostgreSQL fields.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampStatusMixin


class Employee(Base, TimestampStatusMixin):
    """A directory entry mapping a name to an owner key (employee_id)."""

    __tablename__ = "employees"

    # The DISPLAY name (used in the email/FCM greeting).
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Optional schedule alias: when the collaboration schedule distinguishes two
    # same-named people (e.g. "Sneha Mondal" vs "Sneha Mondal (Intern)"), this
    # holds the exact schedule label to match collaboration_days.employee_name.
    # NULL means the schedule uses the display ``name`` verbatim. The greeting
    # always uses ``name``, never this alias.
    schedule_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Owner key.
    employee_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Employee(id={self.id!r}, name={self.name!r}, "
            f"employee_id={self.employee_id!r})"
        )
