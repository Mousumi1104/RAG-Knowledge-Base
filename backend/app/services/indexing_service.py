"""Indexing service: the end-to-end document indexing pipeline.

Responsibilities:

- :func:`index_document` — extract -> chunk -> embed -> upsert vectors -> persist
  Chunk rows, driving the indexing-status lifecycle
  (``pending`` -> ``processing`` -> ``indexed`` | ``failed``).
- :func:`reindex_document` — clear existing vectors/chunks, then re-index.
- :func:`delete_document_vectors` — remove a document's vectors and soft-delete
  its Chunk rows (used by the soft-delete flow).

Only ACTIVE content is indexed/searched. Errors are caught and surfaced via the
document's ``indexing_status``/``error`` rather than crashing the request.

The embedding provider and vector store are constructed lazily and cached at
module level so repeated indexing calls reuse the same (potentially expensive)
clients.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.base import Status
from app.models.chunk import Chunk
from app.models.document import Document
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.services import chunking, extraction
from app.utils.logging import get_logger
from app.vectorstore import VectorStore, get_vector_store

logger = get_logger(__name__)

# Lazy module-level singletons (built on first use, reused afterwards).
_embedding_provider: EmbeddingProvider | None = None
_vector_store: VectorStore | None = None


def _get_embedding_provider() -> EmbeddingProvider:
    global _embedding_provider
    if _embedding_provider is None:
        _embedding_provider = get_embedding_provider()
    return _embedding_provider


def _get_vector_store() -> VectorStore:
    global _vector_store
    if _vector_store is None:
        _vector_store = get_vector_store()
    return _vector_store


def reset_caches() -> None:
    """Drop cached provider/store singletons (used by tests after config change)."""
    global _embedding_provider, _vector_store
    _embedding_provider = None
    _vector_store = None


def _load_document(db: Session, document_id: int) -> Document | None:
    """Load an ACTIVE document by id, or ``None`` if missing/deleted."""
    stmt = select(Document).where(
        Document.id == document_id,
        Document.status == Status.ACTIVE.value,
    )
    return db.execute(stmt).scalars().first()


def _existing_chunks(db: Session, document_id: int) -> list[Chunk]:
    """Return all (any-status) Chunk rows for a document."""
    stmt = select(Chunk).where(Chunk.document_id == document_id)
    return list(db.execute(stmt).scalars().all())


def _clear_document_chunks(db: Session, document_id: int) -> None:
    """Hard-delete Chunk rows for a document (used on reindex)."""
    for chunk in _existing_chunks(db, document_id):
        db.delete(chunk)
    db.flush()


def index_document(db: Session, document_id: int) -> Document | None:
    """Run the full indexing pipeline for one document.

    Lifecycle: sets ``processing`` -> on success ``indexed`` (with ``chunk_count``)
    -> on any failure ``failed`` (with ``error`` message). Incremental: if the
    document already has chunks/vectors they are cleared first so re-running is
    idempotent.

    Returns the updated :class:`Document`, or ``None`` if it was missing/deleted.
    """
    document = _load_document(db, document_id)
    if document is None:
        logger.warning("index_document: document %s not found/active", document_id)
        return None

    document.indexing_status = "processing"
    document.error = None
    db.add(document)
    db.commit()
    logger.info("Indexing document id=%s filename=%s", document.id, document.filename)

    try:
        # Incremental: clear any prior vectors/chunks for a clean re-run.
        store = _get_vector_store()
        store.delete_by_document(document.id)
        _clear_document_chunks(db, document.id)

        pages = extraction.extract_pages(document.file_path, document.document_type)
        chunks = chunking.chunk_pages(pages, doc_id=document.id)

        if not chunks:
            document.indexing_status = "indexed"
            document.chunk_count = 0
            db.add(document)
            db.commit()
            db.refresh(document)
            logger.info("Document id=%s produced no chunks (empty)", document.id)
            return document

        provider = _get_embedding_provider()
        contents = [chunk["content"] for chunk in chunks]
        embeddings = provider.embed_texts(contents)

        ids = [chunk["chunk_id"] for chunk in chunks]
        metadatas = [
            {
                "document_id": int(document.id),
                "chunk_id": chunk["chunk_id"],
                "position": int(chunk["position"]),
                "page": chunk.get("page"),
                "content": chunk["content"],
            }
            for chunk in chunks
        ]

        # Embeddings live on the chunk rows, so the rows must exist (and be
        # committed) before the store — which uses its own session/connection —
        # writes vectors onto them by chunk_id.
        for chunk in chunks:
            db.add(
                Chunk(
                    document_id=document.id,
                    chunk_id=chunk["chunk_id"],
                    content=chunk["content"],
                    position=int(chunk["position"]),
                    page=chunk.get("page"),
                    status=Status.ACTIVE.value,
                )
            )
        db.flush()
        db.commit()

        store.add(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=contents,
        )

        document.indexing_status = "indexed"
        document.chunk_count = len(chunks)
        document.error = None
        db.add(document)
        db.commit()
        db.refresh(document)
        logger.info("Indexed document id=%s with %s chunks", document.id, len(chunks))
        return document

    except Exception as exc:  # noqa: BLE001 - any failure marks the doc failed
        logger.exception("Indexing failed for document %s: %s", document_id, exc)
        db.rollback()
        # Best-effort status update on a clean session state.
        failed = _load_document(db, document_id)
        if failed is not None:
            failed.indexing_status = "failed"
            failed.error = str(exc)[:2000]
            db.add(failed)
            db.commit()
            db.refresh(failed)
            return failed
        return None


def reindex_document(db: Session, document_id: int) -> Document | None:
    """Clear existing vectors/chunks for a document and re-index it."""
    document = _load_document(db, document_id)
    if document is None:
        logger.warning("reindex_document: document %s not found/active", document_id)
        return None

    try:
        _get_vector_store().delete_by_document(document.id)
    except Exception as exc:  # noqa: BLE001 - cleanup must not block reindex
        logger.warning("reindex_document: vector cleanup failed for %s: %s", document_id, exc)
    _clear_document_chunks(db, document_id)
    db.commit()

    return index_document(db, document_id)


def delete_document_vectors(db: Session, document_id: int) -> None:
    """Remove a document's vectors and soft-delete its Chunk rows.

    Used by the soft-delete flow. Robust: vector-store failures are logged but do
    not raise, so a delete is never blocked by the store being unavailable.
    """
    try:
        _get_vector_store().delete_by_document(document_id)
    except Exception as exc:  # noqa: BLE001 - cleanup must not break soft delete
        logger.warning(
            "delete_document_vectors: vector removal failed for %s: %s",
            document_id,
            exc,
        )

    for chunk in _existing_chunks(db, document_id):
        chunk.status = Status.DELETED.value
        db.add(chunk)
    db.commit()
    logger.info("Removed vectors and soft-deleted chunks for document %s", document_id)
