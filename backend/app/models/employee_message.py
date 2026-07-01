"""SQLAlchemy ORM model for Employee-Zone chat messages.

A message belongs to an
:class:`~app.models.employee_conversation.EmployeeConversation` and holds a single
chat turn (``user`` or ``assistant``). Assistant messages may carry the grounding
``sources`` as a JSONB array; user messages leave it ``NULL``. ``employee_id`` is
denormalised for indexing and defence-in-depth ownership checks. Reuses
:class:`TimestampStatusMixin` for the common PostgreSQL fields.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampStatusMixin


class EmployeeMessage(Base, TimestampStatusMixin):
    """A single Employee-Zone chat turn within a conversation."""

    __tablename__ = "employee_messages"

    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "employee_conversations.id",
            name="fk_employee_messages_conversation_id",
        ),
        nullable=False,
        index=True,
    )
    # Denormalised owner (matches the parent conversation's employee_id).
    employee_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Message author role: 'user' | 'assistant'.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # JSONB array of {document, excerpt, document_id, page}; NULL for user turns.
    sources: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        # Serves oldest->newest message reads within a conversation.
        Index("ix_employee_messages_conv_order", "conversation_id", "id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"EmployeeMessage(id={self.id!r}, conversation_id={self.conversation_id!r}, "
            f"role={self.role!r})"
        )
