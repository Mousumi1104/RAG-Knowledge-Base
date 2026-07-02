"""SQLAlchemy ORM model for Employee-Zone push notification device tokens.

One active push token per Employee-Zone owner. ``owner`` is the employee-zone
owner key (the ``X-Employee-Id`` header, falling back to the resolved email) and
is UNIQUE so a device token can be upserted per owner (one active token per
employee). Logout/unregister soft-deletes the row (``status = DELETED``); the
reminder job treats only ``status = ACTIVE`` tokens as "logged in". Reuses
:class:`TimestampStatusMixin` for the common PostgreSQL fields.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampStatusMixin


class DeviceToken(Base, TimestampStatusMixin):
    """A push-notification device token owned by an Employee-Zone owner key."""

    __tablename__ = "device_tokens"

    # Employee-zone owner key (X-Employee-Id else email); UNIQUE enables
    # upsert-by-owner / one active token per employee.
    owner: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    token: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"DeviceToken(id={self.id!r}, owner={self.owner!r})"
