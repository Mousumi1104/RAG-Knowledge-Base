"""pgvector-backed :class:`VectorStore` implementation.

Embeddings live directly on the ``chunks`` table (``Chunk.embedding``), so this
store operates on that table via SQLAlchemy. Cosine distance is computed with
pgvector's ``<=>`` operator and converted to a normalized similarity
(``score = 1.0 - distance``) so callers always see "higher is more relevant",
preserving the original similarity-score semantics expected by the search layer.

The store has no per-request session handle (the :class:`VectorStore` interface
does not pass one), so it opens its own short-lived sessions via
``SessionLocal`` for its operations.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Integer, false, func, or_, select, text, update
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import settings
from app.database.base import Status
from app.database.session import SessionLocal
from app.models.chunk import Chunk
from app.models.document import Document
from app.utils.errors import ProviderError
from app.utils.logging import get_logger
from app.vectorstore.base import QueryResult, VectorStore

logger = get_logger(__name__)


def _ownership_clause(owner_id: int | None, include_global: bool) -> ColumnElement[bool]:
    """Build the DEFAULT-DENY tenant-isolation WHERE clause for a retrieval.

    A candidate chunk's document is allowed iff it is owned by ``owner_id`` OR
    (when ``include_global``) it is an admin-global document. There is NO
    "all documents" fallback: if ``owner_id is None`` and ``include_global`` is
    False this returns a literal ``false`` so the result set is empty (a missing
    scope must never silently leak every tenant's chunks).
    """
    clauses: list[ColumnElement[bool]] = []
    if owner_id is not None:
        clauses.append(Document.owner_id == owner_id)
    if include_global:
        clauses.append(Document.is_global.is_(True))
    if not clauses:
        # Default-deny: no owner scope and globals excluded -> match nothing.
        return false()
    return or_(*clauses)


class PgVectorStore(VectorStore):
    """pgvector vector store operating on the ``chunks`` table.

    Vectors are co-located with chunk rows; chunk rows are expected to already
    exist (written + flushed by the indexing service) before :meth:`add` runs.
    """

    def add(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None:
        """Write embeddings onto the matching ``chunks`` rows, keyed by chunk_id.

        Uses a raw ORM-bulk UPDATE per chunk_id and explicitly sets
        ``last_modified = now()`` (ORM ``onupdate`` does not fire for bulk/Core
        UPDATE statements).
        """
        if not ids:
            return
        try:
            with SessionLocal() as session:
                for chunk_id, embedding in zip(ids, embeddings, strict=False):
                    session.execute(
                        update(Chunk)
                        .where(Chunk.chunk_id == chunk_id)
                        .values(embedding=embedding, last_modified=func.now())
                    )
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("pgvector add failed: %s", exc)
            raise ProviderError("Vector store add failed") from exc

    def query(
        self,
        embedding: list[float],
        top_k: int,
        *,
        owner_id: int | None = None,
        include_global: bool = True,
    ) -> list[QueryResult]:
        """Return up to ``top_k`` most-similar ACTIVE chunks, best first.

        Cosine similarity via the ``<=>`` operator; only ACTIVE chunks belonging
        to ACTIVE documents with a non-null embedding are considered. Score is
        ``1.0 - distance`` to match the expected similarity-score semantics.

        Tenant isolation is enforced in SQL (DEFAULT-DENY): only chunks whose
        document is owned by ``owner_id`` OR (when ``include_global``) is an
        admin-global document are candidates. With ``owner_id is None`` and
        ``include_global is False`` the result set is empty.
        """
        if top_k <= 0:
            return []
        try:
            with SessionLocal() as session:
                distance = Chunk.embedding.cosine_distance(embedding).label("distance")
                stmt = (
                    select(
                        Chunk.chunk_id,
                        Chunk.document_id,
                        Chunk.position,
                        Chunk.page,
                        Chunk.content,
                        Document.owner_id,
                        Document.is_global,
                        distance,
                    )
                    .join(Document, Document.id == Chunk.document_id)
                    .where(
                        Chunk.embedding.isnot(None),
                        Chunk.status == Status.ACTIVE.value,
                        Document.status == Status.ACTIVE.value,
                        _ownership_clause(owner_id, include_global),
                    )
                    .order_by(distance.asc())
                    .limit(top_k)
                )
                rows = session.execute(stmt).all()
        except Exception as exc:  # noqa: BLE001
            logger.error("pgvector query failed: %s", exc)
            raise ProviderError("Vector store query failed") from exc

        results: list[QueryResult] = []
        for chunk_id, document_id, position, page, content, doc_owner_id, doc_is_global, dist in rows:
            results.append(
                QueryResult(
                    id=str(chunk_id),
                    # Cosine distance -> similarity (higher is more relevant).
                    score=1.0 - float(dist),
                    metadata={
                        "document_id": int(document_id),
                        "chunk_id": str(chunk_id),
                        "position": int(position),
                        "page": int(page) if page is not None else None,
                        "content": content or "",
                        "owner_id": int(doc_owner_id) if doc_owner_id is not None else None,
                        "is_global": bool(doc_is_global),
                    },
                    document=content or "",
                )
            )
        # Ordered by ascending distance == descending score (most relevant first).
        return results

    def keyword_query(
        self,
        embedding: list[float],
        terms: list[str],
        top_k: int,
        *,
        owner_id: int | None = None,
        include_global: bool = True,
    ) -> list[QueryResult]:
        """Return up to ``top_k`` ACTIVE chunks literally containing any ``term``.

        Substring match via SQL ``ILIKE`` over chunk content, ordered by best
        vector similarity to ``embedding`` so the most on-topic literal matches
        come first. Each hit carries its real ``1.0 - cosine_distance`` score so
        the caller can blend/threshold it exactly like a vector hit. This rescues
        exact terms (e.g. a "transpose" definition) that the IDF-free embedder
        ranks far below the vector top_k.

        The SAME DEFAULT-DENY tenant-isolation clause as :meth:`query` is applied,
        so this lexical-rescue path can never surface another tenant's chunks.
        """
        cleaned = [t.strip() for t in terms if t and t.strip()]
        if top_k <= 0 or not cleaned:
            return []
        try:
            with SessionLocal() as session:
                distance = Chunk.embedding.cosine_distance(embedding).label("distance")
                like_clauses = [Chunk.content.ilike(f"%{t}%") for t in cleaned]
                # Count how many distinct query terms each chunk literally
                # contains. A chunk matching MORE terms is a far better lexical
                # hit than one matching a single common word (e.g. "matrix"),
                # so we order by match-count first, then vector similarity. This
                # surfaces the chunk that contains ALL query terms (e.g. the
                # transpose *definition*) ahead of chunks that merely mention a
                # common term, even though both pass the ILIKE filter.
                match_count = sum(
                    (Chunk.content.ilike(f"%{t}%")).cast(Integer) for t in cleaned
                ).label("match_count")
                stmt = (
                    select(
                        Chunk.chunk_id,
                        Chunk.document_id,
                        Chunk.position,
                        Chunk.page,
                        Chunk.content,
                        Document.owner_id,
                        Document.is_global,
                        distance,
                    )
                    .join(Document, Document.id == Chunk.document_id)
                    .where(
                        Chunk.embedding.isnot(None),
                        Chunk.status == Status.ACTIVE.value,
                        Document.status == Status.ACTIVE.value,
                        _ownership_clause(owner_id, include_global),
                        or_(*like_clauses),
                    )
                    .order_by(match_count.desc(), distance.asc())
                    .limit(top_k)
                )
                rows = session.execute(stmt).all()
        except Exception as exc:  # noqa: BLE001
            logger.error("pgvector keyword_query failed: %s", exc)
            raise ProviderError("Vector store keyword query failed") from exc

        results: list[QueryResult] = []
        for chunk_id, document_id, position, page, content, doc_owner_id, doc_is_global, dist in rows:
            results.append(
                QueryResult(
                    id=str(chunk_id),
                    score=1.0 - float(dist),
                    metadata={
                        "document_id": int(document_id),
                        "chunk_id": str(chunk_id),
                        "position": int(position),
                        "page": int(page) if page is not None else None,
                        "content": content or "",
                        "owner_id": int(doc_owner_id) if doc_owner_id is not None else None,
                        "is_global": bool(doc_is_global),
                    },
                    document=content or "",
                )
            )
        return results

    def delete_by_document(self, document_id: int) -> None:
        """Clear embeddings for a document's chunks (used on reindex).

        Soft-deleted chunks are already excluded from :meth:`query`/:meth:`count`
        by the status filter; this nulls the vector so reindexing starts clean.
        """
        try:
            with SessionLocal() as session:
                session.execute(
                    update(Chunk)
                    .where(Chunk.document_id == int(document_id))
                    .values(embedding=None, last_modified=func.now())
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("pgvector delete failed for document %s: %s", document_id, exc)
            raise ProviderError("Vector store delete failed") from exc

    def count(self) -> int:
        """Return the number of ACTIVE chunks that carry an embedding."""
        try:
            with SessionLocal() as session:
                stmt = (
                    select(func.count())
                    .select_from(Chunk)
                    .where(
                        Chunk.embedding.isnot(None),
                        Chunk.status == Status.ACTIVE.value,
                    )
                )
                return int(session.execute(stmt).scalar_one())
        except Exception as exc:  # noqa: BLE001
            logger.error("pgvector count failed: %s", exc)
            raise ProviderError("Vector store count failed") from exc

    def health(self) -> dict[str, Any]:
        """Return a small health/status dict; must not raise."""
        try:
            with SessionLocal() as session:
                # Touch the connection to confirm the vector store is reachable.
                session.execute(text("SELECT 1"))
            count = self.count()
            return {
                "backend": "pgvector",
                "extension": "vector",
                "table": "chunks",
                "dim": settings.EMBEDDING_DIM,
                "count": count,
                "status": "ok",
            }
        except Exception as exc:  # noqa: BLE001 - health must not raise
            return {
                "backend": "pgvector",
                "extension": "vector",
                "table": "chunks",
                "status": "error",
                "error": str(exc),
            }
