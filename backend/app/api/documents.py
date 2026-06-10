"""Document management API routes.

Thin router: parse/validate input, delegate to ``document_service``, shape JSON.
Indexing is triggered through a guarded hook that is a safe no-op until the
indexing phase provides the real implementation.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.security import get_current_admin
from app.database.session import get_db
from app.schemas.document import (
    DocumentList,
    DocumentOut,
    MessageResponse,
    ReindexResponse,
)
from app.services import document_service
from app.utils import cloud_storage
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Media types for inline (browser-rendered) serving of stored documents. The
# DOCX type is the OOXML wordprocessing MIME; others fall back to octet-stream.
_MEDIA_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Public routes: no authentication. Anonymous upload plus public file serving so
# the frontend can deep-link to a document (and a #page=N anchor for PDFs).
public_router = APIRouter(
    prefix="/documents",
    tags=["documents"],
)

# Management routes require a valid admin JWT.
admin_router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(get_current_admin)],
)


def _trigger_indexing(db: Session, doc_id: int, *, reindex: bool = False) -> None:
    """Guarded hook to kick off (re)indexing for a document.

    Delegates to ``app.services.indexing_service``. Indexing runs synchronously
    (fine for the POC); any failure is caught and logged so it cannot fail the
    request — the document is marked ``failed`` by the service instead.
    """
    try:
        from app.services.indexing_service import (  # type: ignore[import-not-found]
            index_document,
            reindex_document,
        )
    except ImportError:
        logger.debug("Indexing service not available; document %s stays pending", doc_id)
        return
    try:
        if reindex:
            reindex_document(db, doc_id)
        else:
            index_document(db, doc_id)
    except Exception as exc:  # noqa: BLE001 - indexing failure must not fail the request
        logger.warning("Indexing trigger failed for document %s: %s", doc_id, exc)


# --- Public routes --------------------------------------------------------
# Public routes are ordered so a literal path like ``/upload`` is never shadowed
# by a ``/{doc_id}/...`` parameterized route. The file route lives on this public
# router (not the admin one), so it resolves without auth even though admin has a
# ``/{doc_id}`` route — FastAPI matches per-router and the path differs.
@public_router.post("/upload", response_model=DocumentOut, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> DocumentOut:
    """Publicly upload a document: validate, store, create the row, trigger indexing.

    This endpoint is intentionally unauthenticated. Uploads are always attributed
    to ``"anonymous"`` — any client-supplied ``uploaded_by`` is ignored so a caller
    cannot spoof ``"admin"``.
    """
    contents = await file.read()
    document = document_service.create_document(
        db,
        upload_file_bytes=contents,
        filename=file.filename or "upload",
        uploaded_by="anonymous",
    )
    _trigger_indexing(db, document.id)
    db.refresh(document)
    return DocumentOut.model_validate(document)


@public_router.get("/{doc_id}/file", response_model=None)
def get_document_file(doc_id: int, db: Session = Depends(get_db)) -> FileResponse | RedirectResponse:
    """Publicly serve an active document's original file for inline rendering.

    Returns the stored file with the correct media type and an ``inline`` content
    disposition so a browser renders it (and PDF viewers honor ``#page=N`` deep
    links). 404 if the document is missing/deleted or its file is gone from disk.
    Intentionally unauthenticated.
    """
    document = document_service.get_document(db, doc_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    # Remote-stored (Cloudinary) files: redirect to the CDN URL for inline render.
    if cloud_storage.is_remote(document.file_path):
        return RedirectResponse(document.file_path, status_code=307)

    path = Path(document.file_path)
    if not path.is_file():
        logger.warning("Document %s file missing on disk: %s", doc_id, document.file_path)
        raise HTTPException(status_code=404, detail=f"Document {doc_id} file not found")

    media_type = _MEDIA_TYPES.get(
        (document.document_type or "").lower().lstrip("."),
        "application/octet-stream",
    )
    return FileResponse(
        path,
        media_type=media_type,
        filename=document.filename,
        content_disposition_type="inline",
    )


# --- Admin (management) routes --------------------------------------------
@admin_router.get("", response_model=DocumentList)
def list_documents(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
) -> DocumentList:
    """List active documents (DELETED/INACTIVE excluded unless requested)."""
    documents = document_service.list_documents(db, include_inactive=include_inactive)
    items = [DocumentOut.model_validate(doc) for doc in documents]
    return DocumentList(items=items, total=len(items))


@admin_router.get("/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: int, db: Session = Depends(get_db)) -> DocumentOut:
    """Fetch one active document (404 if missing/deleted)."""
    document = document_service.get_document_or_404(db, doc_id)
    return DocumentOut.model_validate(document)


@admin_router.delete("/{doc_id}", response_model=MessageResponse)
def delete_document(doc_id: int, db: Session = Depends(get_db)) -> MessageResponse:
    """Soft-delete a document (status = DELETED) and clean up its vectors."""
    document_service.soft_delete_document(db, doc_id)
    return MessageResponse(detail=f"Document {doc_id} deleted")


@admin_router.post("/{doc_id}/reindex", response_model=ReindexResponse)
def reindex_document(doc_id: int, db: Session = Depends(get_db)) -> ReindexResponse:
    """Reset indexing status to pending and trigger the indexing hook."""
    document = document_service.set_indexing_status(db, doc_id, status="pending")
    _trigger_indexing(db, document.id, reindex=True)
    db.refresh(document)
    return ReindexResponse(
        id=document.id,
        indexing_status=document.indexing_status,
        detail="Reindex triggered",
    )
