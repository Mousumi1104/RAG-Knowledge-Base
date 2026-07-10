"""SQLAlchemy ORM model for uploaded documents.

The relational row holds document metadata and indexing state only; the actual
chunk vectors live in the vector store. The model reuses the shared
:class:`TimestampStatusMixin` for the common PostgreSQL fields (``id``,
``status``, ``added_time``, ``last_modified``).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampStatusMixin


class Document(Base, TimestampStatusMixin):
    """A user-uploaded source document and its indexing lifecycle state."""

    __tablename__ = "documents"

    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    document_type: Mapped[str] = mapped_column(String(16), nullable=False)
    uploaded_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True, default="admin", server_default="admin"
    )
    # Owning user. NULL == not user-owned (admin-global or legacy backfilled docs).
    owner_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", name="fk_documents_owner_id_users"),
        nullable=True,
        index=True,
    )
    # True == admin-global doc that silently grounds every user's answers.
    is_global: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # Stable external identity for a synced global doc (e.g. ``"hr_handbook"``).
    # NULL for ordinary user/admin uploads. The HR-admin portal's handbook-sync
    # bridge sets this so a re-upload can REPLACE the prior copy under the same
    # ref (soft-delete old + upload new) instead of piling up duplicate globals.
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    indexing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Document(id={self.id!r}, filename={self.filename!r}, "
            f"indexing_status={self.indexing_status!r})"
        )
