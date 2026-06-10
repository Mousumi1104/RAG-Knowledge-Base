"""SQLAlchemy ORM model for uploaded documents.

The relational row holds document metadata and indexing state only; the actual
chunk vectors live in the vector store. The model reuses the shared
:class:`TimestampStatusMixin` for the common PostgreSQL fields (``id``,
``status``, ``added_time``, ``last_modified``).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String, Text
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
