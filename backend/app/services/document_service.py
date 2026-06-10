"""Document lifecycle business logic.

All document operations (create/list/get/soft-delete/reupload/status) live here so
the API layer stays thin. Vector-store cleanup and indexing are wired via guarded
hooks that are safe no-ops until the indexing phase provides the real
implementation.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.base import Status
from app.models.document import Document
from app.utils import cloud_storage
from app.utils.errors import NotFoundError
from app.utils.files import sanitize_filename, validate_upload
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Resolve the uploads directory relative to the backend root robustly from this
# file's location: app/services/document_service.py -> backend/uploads.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
UPLOADS_DIR = _BACKEND_ROOT / "uploads"


def _ensure_uploads_dir() -> Path:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOADS_DIR


def _store_bytes(data: bytes, filename: str) -> tuple[str, str]:
    """Persist ``data`` and return ``(stored_location, safe_name)``.

    ``stored_location`` is a Cloudinary ``secure_url`` when Cloudinary is
    configured (Render free tier has no persistent disk), otherwise the local
    filesystem path as a string. Either value is what gets saved to
    ``Document.file_path``.
    """
    safe_name = sanitize_filename(filename)
    if settings.cloudinary_enabled:
        url = cloud_storage.upload_bytes(data, safe_name)
        return url, safe_name
    unique = uuid.uuid4().hex
    target = _ensure_uploads_dir() / f"{unique}_{safe_name}"
    target.write_bytes(data)
    return str(target), safe_name


def _cleanup_vectors(db: Session, doc_id: int) -> None:
    """Guarded hook to remove a document's vectors during soft delete.

    Delegates to ``app.services.indexing_service.delete_document_vectors`` when
    available; otherwise a safe no-op. Errors are logged, never raised, so the
    soft delete itself always succeeds.
    """
    try:
        from app.services.indexing_service import (  # type: ignore[import-not-found]
            delete_document_vectors,
        )
    except ImportError:
        logger.debug("Indexing service not available; skipping vector cleanup")
        return
    try:
        delete_document_vectors(db, doc_id)
    except Exception as exc:  # noqa: BLE001 - cleanup must not break soft delete
        logger.warning("Vector cleanup failed for document %s: %s", doc_id, exc)


def create_document(
    db: Session,
    upload_file_bytes: bytes,
    filename: str,
    uploaded_by: str | None = "admin",
    owner_id: int | None = None,
    is_global: bool = False,
) -> Document:
    """Validate, store, and create a new document row (status ACTIVE, pending).

    ``owner_id``/``is_global`` set tenant ownership: a user upload passes
    ``owner_id=<user id>, is_global=False``; an admin global upload passes
    ``owner_id=None, is_global=True``. These are set server-side from the
    authenticated identity and are never client-controlled.
    """
    doc_type = validate_upload(filename, len(upload_file_bytes))
    stored_location, safe_name = _store_bytes(upload_file_bytes, filename)

    document = Document(
        filename=safe_name,
        document_type=doc_type,
        uploaded_by=uploaded_by or "admin",
        owner_id=owner_id,
        is_global=is_global,
        file_path=stored_location,
        size=len(upload_file_bytes),
        indexing_status="pending",
        chunk_count=0,
        error=None,
        status=Status.ACTIVE.value,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    logger.info("Created document id=%s filename=%s", document.id, safe_name)
    return document


def list_documents(
    db: Session,
    include_inactive: bool = False,
    owner_id: int | None = None,
) -> list[Document]:
    """Return documents, excluding DELETED/INACTIVE unless ``include_inactive``.

    Tenant scoping (server-side): when ``owner_id`` is given, only that user's
    own documents are returned (``Document.owner_id == owner_id``) — this is the
    personal library and intentionally excludes admin-global docs. When
    ``owner_id`` is ``None`` (admin path), all documents are returned.
    """
    stmt = select(Document)
    if not include_inactive:
        stmt = stmt.where(Document.status == Status.ACTIVE.value)
    if owner_id is not None:
        stmt = stmt.where(Document.owner_id == owner_id)
    stmt = stmt.order_by(Document.added_time.desc(), Document.id.desc())
    return list(db.execute(stmt).scalars().all())


def get_document(
    db: Session,
    doc_id: int,
    include_inactive: bool = False,
    owner_id: int | None = None,
    allow_global: bool = False,
) -> Document | None:
    """Return one document (active-only by default), or ``None`` if not found.

    Tenant scoping (server-side): when ``owner_id`` is given, the row must be
    owned by that user (or, when ``allow_global`` is set, be an admin-global
    doc); otherwise ``None`` is returned so cross-tenant access is
    indistinguishable from a missing row (no IDOR / existence leak). When
    ``owner_id`` is ``None`` (admin path), any document matches.
    """
    stmt = select(Document).where(Document.id == doc_id)
    if not include_inactive:
        stmt = stmt.where(Document.status == Status.ACTIVE.value)
    if owner_id is not None:
        if allow_global:
            stmt = stmt.where(
                or_(Document.owner_id == owner_id, Document.is_global.is_(True))
            )
        else:
            stmt = stmt.where(Document.owner_id == owner_id)
    return db.execute(stmt).scalars().first()


def get_document_or_404(
    db: Session,
    doc_id: int,
    owner_id: int | None = None,
    allow_global: bool = False,
) -> Document:
    """Return an active (optionally owner-scoped) document or raise NotFoundError.

    A cross-tenant id raises the same 404 as a missing id (no existence leak).
    """
    document = get_document(db, doc_id, owner_id=owner_id, allow_global=allow_global)
    if document is None:
        raise NotFoundError(f"Document {doc_id} not found")
    return document


def soft_delete_document(
    db: Session, doc_id: int, owner_id: int | None = None
) -> Document:
    """Soft-delete a document (status = DELETED) and clean up its vectors.

    Owner-scoped: when ``owner_id`` is given, a user may only delete their own
    docs (global docs are NOT deletable by users — ``allow_global`` is False).
    """
    document = get_document_or_404(db, doc_id, owner_id=owner_id)
    document.status = Status.DELETED.value
    db.add(document)
    db.commit()
    db.refresh(document)

    # Guarded hook: remove vectors from the store and soft-delete chunk rows.
    _cleanup_vectors(db, document.id)

    # Best-effort cleanup of the remote-stored file (local files are left in
    # place on soft delete, matching the original behavior).
    if cloud_storage.is_remote(document.file_path):
        cloud_storage.delete_url(document.file_path)

    logger.info("Soft-deleted document id=%s", doc_id)
    return document


def reupload_document(db: Session, doc_id: int, new_bytes: bytes, filename: str) -> Document:
    """Replace a document's file with a new version and reset indexing state."""
    document = get_document_or_404(db, doc_id)
    doc_type = validate_upload(filename, len(new_bytes))

    stored_location, safe_name = _store_bytes(new_bytes, filename)

    # Best-effort removal of the previous stored file. Remote files go through
    # Cloudinary deletion; local files are unlinked from disk as before.
    old_location = document.file_path
    if old_location and old_location != stored_location:
        if cloud_storage.is_remote(old_location):
            cloud_storage.delete_url(old_location)
        else:
            old_path = Path(old_location)
            if old_path.exists():
                try:
                    old_path.unlink()
                except OSError as exc:
                    logger.warning("Could not remove old file %s: %s", old_path, exc)

    document.filename = safe_name
    document.document_type = doc_type
    document.file_path = stored_location
    document.size = len(new_bytes)
    document.indexing_status = "pending"
    document.chunk_count = 0
    document.error = None
    db.add(document)
    db.commit()
    db.refresh(document)
    logger.info("Re-uploaded document id=%s filename=%s", doc_id, safe_name)
    return document


def set_indexing_status(
    db: Session,
    doc_id: int,
    status: str,
    chunk_count: int | None = None,
    error: str | None = None,
    owner_id: int | None = None,
) -> Document:
    """Update a document's indexing status (and optional chunk_count/error).

    Owner-scoped: when ``owner_id`` is given, a user may only reindex their own
    docs (global docs are not user-reindexable). The background indexer itself
    calls this with ``owner_id=None`` (it operates on a resolved doc id).
    """
    document = get_document_or_404(db, doc_id, owner_id=owner_id)
    document.indexing_status = status
    if chunk_count is not None:
        document.chunk_count = chunk_count
    document.error = error
    db.add(document)
    db.commit()
    db.refresh(document)
    return document
