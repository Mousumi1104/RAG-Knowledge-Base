"""SQLAlchemy ORM model for authenticated users.

A user owns documents and conversations and authenticates by email + password.
The model reuses the shared :class:`TimestampStatusMixin` for the common
PostgreSQL fields (``id``, ``status``, ``added_time``, ``last_modified``);
``added_time`` serves as the ``created_at`` timestamp. Soft delete sets
``status = DELETED``; ``is_active = false`` is a distinct admin disable toggle.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, String, true
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampStatusMixin


class User(Base, TimestampStatusMixin):
    """An authenticated user (registers with email + password)."""

    __tablename__ = "users"

    # Case-insensitive unique identity (CITEXT requires the ``citext`` extension).
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )

    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"User(id={self.id!r}, email={self.email!r}, role={self.role!r})"
