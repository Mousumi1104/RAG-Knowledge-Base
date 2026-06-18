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

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.base import Status
from app.models.chunk import Chunk
from app.models.document import Document
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.services import chunking, extraction
from app.utils.logging import get_logger
from app.vectorstore import VectorStore, get_vector_store

logger = get_logger(__name__)

# Embed/persist this many chunks at a time so peak memory stays bounded to one
# batch (not the whole document) — critical on the 512MB free instance where a
# multi-thousand-chunk PDF would otherwise OOM-kill the worker.
BATCH = 200

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
        doc_id = int(document.id)

        # Process in fixed-size batches so peak memory is ~one batch, never the
        # whole document. For each batch we build only that batch's contents /
        # embeddings / metadatas / Chunk rows, persist them, then drop all
        # references so the batch can be GC'd before the next one is built.
        total = 0
        for start in range(0, len(chunks), BATCH):
            batch = chunks[start : start + BATCH]

            batch_ids = [c["chunk_id"] for c in batch]
            batch_contents = [c["content"] for c in batch]
            batch_embeddings = provider.embed_texts(batch_contents)
            batch_metadatas = [
                {
                    "document_id": doc_id,
                    "chunk_id": c["chunk_id"],
                    "position": int(c["position"]),
                    "page": c.get("page"),
                    "content": c["content"],
                }
                for c in batch
            ]

            # Embeddings live on the chunk rows, so the rows must exist (and be
            # committed) before the store — which uses its own session/connection
            # — writes vectors onto them by chunk_id.
            for c in batch:
                db.add(
                    Chunk(
                        document_id=doc_id,
                        chunk_id=c["chunk_id"],
                        content=c["content"],
                        position=int(c["position"]),
                        page=c.get("page"),
                        status=Status.ACTIVE.value,
                    )
                )
            db.flush()
            db.commit()

            store.add(
                ids=batch_ids,
                embeddings=batch_embeddings,
                metadatas=batch_metadatas,
                documents=batch_contents,
            )

            total += len(batch)
            # Drop per-batch references so this batch is collectible before the
            # next batch is built (keeps peak memory ~one batch).
            del batch, batch_ids, batch_contents, batch_embeddings, batch_metadatas

        document.indexing_status = "indexed"
        document.chunk_count = total
        document.error = None
        db.add(document)
        db.commit()
        db.refresh(document)
        logger.info("Indexed document id=%s with %s chunks", document.id, total)
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


def fail_stale_processing(db: Session, older_than_minutes: int | None = None) -> int:
    """Mark long-stuck ``processing`` documents as ``failed``.

    A worker OOM-kill (SIGKILL) can't be caught, so a document indexing at the
    time stays ``processing`` forever. On startup we sweep ACTIVE documents whose
    ``indexing_status`` is ``processing`` and whose ``last_modified`` (falling back
    to ``added_time``) is older than the threshold, marking them ``failed`` with a
    clear error so they can be retried/reindexed.

    The threshold defaults to ``settings.INDEXING_STALE_MINUTES`` (120) — wide
    enough that a paced Gemini embed job (~24 min for a 240-chunk doc) is never
    mistaken for a dead one — but callers may override it.

    Returns the number of documents updated.
    """
    if older_than_minutes is None:
        older_than_minutes = getattr(settings, "INDEXING_STALE_MINUTES", 120)
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    stmt = select(Document).where(
        Document.status == Status.ACTIVE.value,
        Document.indexing_status == "processing",
    )
    updated = 0
    for document in db.execute(stmt).scalars().all():
        stamp = document.last_modified or document.added_time
        if stamp is not None and stamp > cutoff:
            continue
        document.indexing_status = "failed"
        document.error = "indexing interrupted (worker restarted)"
        db.add(document)
        updated += 1
    if updated:
        db.commit()
        logger.warning("Marked %s stale 'processing' document(s) as failed", updated)
    return updated


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
