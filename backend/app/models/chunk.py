"""SQLAlchemy ORM model for document chunks.

A chunk row holds the chunk's text and its embedding vector (pgvector
``Vector`` column) directly on the row. Reuses :class:`TimestampStatusMixin`
for the common PostgreSQL fields; soft delete sets ``status = DELETED``.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.database.base import Base, TimestampStatusMixin


class Chunk(Base, TimestampStatusMixin):
    """A single chunk of an indexed document."""

    __tablename__ = "chunks"

    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("documents.id"),
        nullable=False,
        index=True,
    )
    # Stable, vector-store record key (e.g. "{document_id}:{position}").
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 1-based source page this chunk came from (e.g. PDF page). Nullable: pre-page
    # rows and page-less formats (txt/md/docx) leave it NULL.
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Embedding vector co-located on the chunk row; dimension MUST equal
    # settings.EMBEDDING_DIM (384). Nullable so a chunk can exist pre-embed.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.EMBEDDING_DIM),
        nullable=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Chunk(id={self.id!r}, document_id={self.document_id!r}, "
            f"chunk_id={self.chunk_id!r}, position={self.position!r}, page={self.page!r})"
        )
