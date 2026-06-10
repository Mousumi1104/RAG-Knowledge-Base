"""SQLAlchemy ORM model for persisted chat conversations.

A conversation groups an ordered series of chat messages. The model reuses the
shared :class:`TimestampStatusMixin` for the common PostgreSQL fields (``id``,
``status``, ``added_time``, ``last_modified``); soft delete sets
``status = DELETED``.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampStatusMixin


class Conversation(Base, TimestampStatusMixin):
    """A persisted chat conversation (a thread of messages)."""

    __tablename__ = "conversations"

    # Auto-derived from the first user message (truncated) or NULL ("New conversation").
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Conversation(id={self.id!r}, title={self.title!r})"
