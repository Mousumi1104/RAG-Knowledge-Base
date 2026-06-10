"""Admin service: aggregate stats, indexing diagnostics, reindex, and health.

All business logic for the admin API lives here so the router stays thin. Every
query respects the soft-delete lifecycle: counts and overviews consider only
ACTIVE (non-deleted, non-inactive) documents and chunks. Vector-store and
database health checks are defensive and never raise.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.database.base import Status
from app.models.chunk import Chunk
from app.models.document import Document
from app.services import indexing_service
from app.utils.logging import get_logger
from app.vectorstore import get_vector_store

logger = get_logger(__name__)


def _vector_store_health() -> dict[str, Any]:
    """Return vector-store health, never raising."""
    try:
        return get_vector_store().health()
    except Exception as exc:  # noqa: BLE001 - health must not raise
        logger.warning("Vector store health check failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def _vector_count() -> int | None:
    """Return total stored vectors, or ``None`` if the store is unavailable."""
    try:
        return int(get_vector_store().count())
    except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
        logger.warning("Vector store count failed: %s", exc)
        return None


def get_stats(db: Session) -> dict[str, Any]:
    """Return aggregate stats over ACTIVE (non-deleted) documents.

    Includes total document/chunk counts, a breakdown of documents by
    ``indexing_status`` (indexed/failed/pending/processing), plus vector-store
    health and vector count.
    """
    total_documents = db.execute(
        select(func.count()).select_from(Document).where(Document.status == Status.ACTIVE.value)
    ).scalar_one()

    total_chunks = db.execute(
        select(func.count()).select_from(Chunk).where(Chunk.status == Status.ACTIVE.value)
    ).scalar_one()

    status_rows = db.execute(
        select(Document.indexing_status, func.count())
        .where(Document.status == Status.ACTIVE.value)
        .group_by(Document.indexing_status)
    ).all()
    by_status = {str(name): int(count) for name, count in status_rows}

    stats: dict[str, Any] = {
        "total_documents": int(total_documents),
        "total_chunks": int(total_chunks),
        "indexed": by_status.get("indexed", 0),
        "failed": by_status.get("failed", 0),
        "pending": by_status.get("pending", 0),
        "processing": by_status.get("processing", 0),
        "vector_db_health": _vector_store_health(),
        "vector_count": _vector_count(),
    }
    logger.info(
        "admin.get_stats: documents=%s chunks=%s",
        stats["total_documents"],
        stats["total_chunks"],
    )
    return stats


def get_indexing_status_overview(db: Session) -> dict[str, Any]:
    """Return an indexing breakdown plus per-document detail for ACTIVE docs."""
    status_rows = db.execute(
        select(Document.indexing_status, func.count())
        .where(Document.status == Status.ACTIVE.value)
        .group_by(Document.indexing_status)
    ).all()
    counts = {str(name): int(count) for name, count in status_rows}

    docs = (
        db.execute(
            select(Document).where(Document.status == Status.ACTIVE.value).order_by(Document.id)
        )
        .scalars()
        .all()
    )
    documents = [
        {
            "id": int(doc.id),
            "filename": doc.filename,
            "indexing_status": doc.indexing_status,
            "chunk_count": int(doc.chunk_count),
            "error": doc.error,
        }
        for doc in docs
    ]

    return {
        "counts": counts,
        "total_documents": len(documents),
        "documents": documents,
    }


def trigger_reindex(db: Session, document_id: int | None) -> dict[str, Any]:
    """Reindex a single ACTIVE document, or all ACTIVE documents if ``None``.

    Returns a summary ``{reindexed: [...ids], failed: [...], count}``. A failure
    entry is ``{"id": <id>, "error": <message>}``. Individual failures never abort
    the batch.
    """
    if document_id is not None:
        target_ids = [int(document_id)]
    else:
        target_ids = [
            int(row)
            for row in db.execute(
                select(Document.id)
                .where(Document.status == Status.ACTIVE.value)
                .order_by(Document.id)
            )
            .scalars()
            .all()
        ]

    reindexed: list[int] = []
    failed: list[dict[str, Any]] = []

    for doc_id in target_ids:
        try:
            document = indexing_service.reindex_document(db, doc_id)
            if document is None:
                failed.append({"id": doc_id, "error": "not found or not active"})
            elif document.indexing_status == "failed":
                failed.append({"id": doc_id, "error": document.error or "indexing failed"})
            else:
                reindexed.append(doc_id)
        except Exception as exc:  # noqa: BLE001 - one failure must not abort batch
            logger.exception("Reindex failed for document %s: %s", doc_id, exc)
            db.rollback()
            failed.append({"id": doc_id, "error": str(exc)})

    summary = {
        "reindexed": reindexed,
        "failed": failed,
        "count": len(reindexed),
    }
    logger.info(
        "admin.trigger_reindex: reindexed=%s failed=%s",
        len(reindexed),
        len(failed),
    )
    return summary


def health(db: Session) -> dict[str, Any]:
    """Check DB connectivity and vector-store health.

    Returns ``{database, vector_db, status}`` where ``status`` is ``"ok"`` only
    when both the database and the vector store report healthy, else
    ``"degraded"``.
    """
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # noqa: BLE001 - health must not raise
        logger.warning("Database health check failed: %s", exc)
        database = f"error: {exc}"

    vector_db = _vector_store_health()
    healthy = database == "ok" and vector_db.get("status") == "ok"

    return {
        "database": database,
        "vector_db": vector_db,
        "status": "ok" if healthy else "degraded",
    }
