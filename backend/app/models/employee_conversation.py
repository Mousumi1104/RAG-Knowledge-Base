"""SQLAlchemy ORM model for Employee-Zone chat conversations.

Employee-Zone chat history lives in its OWN tables (separate from the admin/user
``conversations``/``messages`` tables) so it can be owned by a string
``employee_id`` and evolved independently. A conversation groups an ordered
series of :class:`~app.models.employee_message.EmployeeMessage` turns and reuses
the shared :class:`TimestampStatusMixin` (``id``, ``status``, ``added_time``,
``last_modified``); soft delete sets ``status = DELETED``.
"""

from __future__ import annotations

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampStatusMixin


class EmployeeConversation(Base, TimestampStatusMixin):
    """A persisted Employee-Zone chat thread, owned by ``employee_id``."""

    __tablename__ = "employee_conversations"

    # Owner key. In Mode A this is the (unverified) ``X-Employee-Id`` header,
    # falling back to the resolved employee email when the id header is absent.
    employee_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Stored identity attributes (not the owner key); informational.
    employee_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    employee_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Auto-derived from the first user message (truncated) or NULL ("New conversation").
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Denormalised message count, bumped on each persisted turn (cheap list reads).
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        # Serves the per-employee, newest-first list (keyset on last_modified, id).
        Index(
            "ix_employee_conversations_owner_recent",
            "employee_id",
            "last_modified",
            "id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"EmployeeConversation(id={self.id!r}, employee_id={self.employee_id!r}, "
            f"title={self.title!r})"
        )
