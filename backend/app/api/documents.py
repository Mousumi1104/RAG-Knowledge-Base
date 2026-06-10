"""Document management API routes.

Thin router: parse/validate input, delegate to ``document_service``, shape JSON.
Indexing is triggered through a guarded hook that is a safe no-op until the
indexing phase provides the real implementation.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.core.security import get_current_admin
from app.database.session import SessionLocal, get_db
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


def _run_indexing_bg(doc_id: int, *, reindex: bool = False) -> None:
    """Run (re)indexing for a document in the background.

    Scheduled via ``BackgroundTasks`` so it runs *after* the response is sent.
    Because this is a plain ``def`` (not ``async``), Starlette executes it in a
    worker threadpool, so heavy CPU/IO indexing never blocks the event loop (or
    ``/health``).

    It opens its **own** short-lived DB session: the request-scoped ``db`` is
    already closed by the time this runs, so it must not be reused. Any failure
    is caught and logged — the indexing service marks the document ``failed``.
    """
    try:
        from app.services.indexing_service import (  # type: ignore[import-not-found]
            index_document,
            reindex_document,
        )
    except ImportError:
        logger.debug("Indexing service not available; document %s stays pending", doc_id)
        return

    session = SessionLocal()
    try:
        if reindex:
            reindex_document(session, doc_id)
        else:
            index_document(session, doc_id)
        session.commit()
    except Exception as exc:  # noqa: BLE001 - background indexing must never crash the worker
        logger.warning("Background indexing failed for document %s: %s", doc_id, exc)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - best-effort rollback
            pass
    finally:
        session.close()


# --- Public routes --------------------------------------------------------
# Public routes are ordered so a literal path like ``/upload`` is never shadowed
# by a ``/{doc_id}/...`` parameterized route. The file route lives on this public
# router (not the admin one), so it resolves without auth even though admin has a
# ``/{doc_id}`` route — FastAPI matches per-router and the path differs.
@public_router.post("/upload", response_model=DocumentOut, status_code=201)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> DocumentOut:
    """Publicly upload a document: validate, store, create the row, queue indexing.

    This endpoint is intentionally unauthenticated. Uploads are always attributed
    to ``"anonymous"`` — any client-supplied ``uploaded_by`` is ignored so a caller
    cannot spoof ``"admin"``.

    Size/type validation happens inside ``create_document`` (``validate_upload``)
    *before* the file is stored, so an oversized file is rejected with 413 quickly.
    Indexing is scheduled as a background task and the row is returned immediately
    (status ``pending``/``processing``) so a large document never times out the
    request.
    """
    contents = await file.read()
    document = document_service.create_document(
        db,
        upload_file_bytes=contents,
        filename=file.filename or "upload",
        uploaded_by="anonymous",
    )
    # Snapshot the response payload before the request session closes, then run
    # indexing in the background (own session) so the response returns instantly.
    result = DocumentOut.model_validate(document)
    background_tasks.add_task(_run_indexing_bg, document.id)
    return result


@public_router.get("/{doc_id}/file", response_model=None)
def get_document_file(doc_id: int, db: Session = Depends(get_db)) -> FileResponse | StreamingResponse:
    """Publicly serve an active document's original file for inline rendering.

    Returns the stored file with the correct media type and an ``inline`` content
    disposition so a browser renders it (and PDF viewers honor ``#page=N`` deep
    links). 404 if the document is missing/deleted or its file is gone from disk.
    Intentionally unauthenticated.
    """
    document = document_service.get_document(db, doc_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    media_type = _MEDIA_TYPES.get(
        (document.document_type or "").lower().lstrip("."),
        "application/octet-stream",
    )

    # Remote-stored (Cloudinary) files: stream the bytes back THROUGH the backend
    # with inline headers rather than redirecting. Cloudinary serves ``raw``
    # resources as ``application/octet-stream`` with a ``Content-Disposition:
    # attachment`` (forcing a download) and a 307 redirect also drops the
    # ``#page=N`` fragment. Proxying keeps the URL on our origin so the fragment
    # survives and the browser renders the file inline at the right page.
    if cloud_storage.is_remote(document.file_path):
        url = document.file_path
        try:
            client = httpx.Client(follow_redirects=True, timeout=30.0)
            upstream = client.stream("GET", url)
            response = upstream.__enter__()
            if response.status_code != 200:
                upstream.__exit__(None, None, None)
                client.close()
                logger.warning("Upstream fetch for document %s returned %s", doc_id, response.status_code)
                raise HTTPException(status_code=502, detail=f"Document {doc_id} source is unavailable")
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - upstream failures become a 502
            logger.warning("Failed to fetch remote file for document %s: %s", doc_id, exc)
            raise HTTPException(status_code=502, detail=f"Document {doc_id} source is unavailable") from exc

        def _iter() -> "object":
            try:
                yield from response.iter_bytes()
            finally:
                upstream.__exit__(None, None, None)
                client.close()

        return StreamingResponse(
            _iter(),
            media_type=media_type,
            headers={"Content-Disposition": f'inline; filename="{document.filename}"'},
        )

    path = Path(document.file_path)
    if not path.is_file():
        logger.warning("Document %s file missing on disk: %s", doc_id, document.file_path)
        raise HTTPException(status_code=404, detail=f"Document {doc_id} file not found")

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
def reindex_document(
    doc_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ReindexResponse:
    """Reset indexing status to pending and queue reindexing in the background."""
    document = document_service.set_indexing_status(db, doc_id, status="pending")
    response = ReindexResponse(
        id=document.id,
        indexing_status=document.indexing_status,
        detail="Reindex triggered",
    )
    background_tasks.add_task(_run_indexing_bg, document.id, reindex=True)
    return response
