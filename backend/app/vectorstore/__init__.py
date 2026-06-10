"""Vector store package and config-driven factory."""

from __future__ import annotations

from app.core.config import settings
from app.utils.logging import get_logger
from app.vectorstore.base import QueryResult, VectorStore

logger = get_logger(__name__)

__all__ = ["QueryResult", "VectorStore", "get_vector_store"]


def get_vector_store() -> VectorStore:
    """Return a :class:`VectorStore` selected by ``settings.VECTOR_DB``.

    Defaults to pgvector. Unknown values warn and fall back to pgvector.
    """
    backend = (settings.VECTOR_DB or "pgvector").strip().lower()
    if backend not in ("pgvector", "pgvec", "postgres"):
        logger.warning("Unknown VECTOR_DB=%r; falling back to pgvector.", backend)

    from app.vectorstore.pgvector_store import PgVectorStore

    return PgVectorStore()
