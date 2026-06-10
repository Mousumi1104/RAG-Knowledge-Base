"""SQLAlchemy ORM model for persisted chat messages.

A message belongs to a :class:`~app.models.conversation.Conversation` and holds a
single chat turn (``user`` or ``assistant``). Assistant messages may carry the
grounding ``sources`` as a JSONB array; user messages leave it ``NULL``. Reuses
:class:`TimestampStatusMixin` for the common PostgreSQL fields.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampStatusMixin


class Message(Base, TimestampStatusMixin):
    """A single chat turn within a conversation."""

    __tablename__ = "messages"

    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("conversations.id"),
        nullable=False,
        index=True,
    )
    # Message author role: 'user' | 'assistant'.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # JSONB array of {document, excerpt, document_id, score}; NULL for user turns.
    sources: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Message(id={self.id!r}, conversation_id={self.conversation_id!r}, "
            f"role={self.role!r})"
        )
