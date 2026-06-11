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

from app.core.security import (
    get_current_admin,
    get_current_user,
    get_current_user_query_or_header,
)
from app.database.session import SessionLocal, get_db
from app.models.user import User
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

# User routes: require an authenticated user (get_current_user). Every route here
# is tenant-scoped server-side — a user only ever sees/acts on their own documents
# (admins, resolved via role at request time, act on all). Uploads, the personal
# library list/get/delete/reindex, and authenticated file serving live here.
#
# NOTE: this router is still exported as ``public_router`` for main.py's
# ``include_router`` call, but it is no longer public — anonymous access is gone.
public_router = APIRouter(
    prefix="/documents",
    tags=["documents"],
)

# Admin-only management routes (e.g. global uploads) require a valid admin JWT.
admin_router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(get_current_admin)],
)


def _owner_scope(current_user: User) -> int | None:
    """Resolve the document-ownership scope for a request.

    Returns ``None`` for admins (no scope → all documents) and the user's id for
    everyone else (scope → own documents only). Centralizes the admin-sees-all
    rule so every route applies it identically.
    """
    return None if current_user.role == "admin" else current_user.id


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
    current_user: User = Depends(get_current_user),
) -> DocumentOut:
    """Upload a user-owned document: validate, store, create the row, queue indexing.

    Requires authentication (no more anonymous uploads). The document is owned by
    the authenticated user (``owner_id = current_user.id``, ``is_global = False``)
    and attributed to their email. Ownership is set server-side from the token and
    is never client-controlled.

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
        uploaded_by=current_user.email,
        owner_id=current_user.id,
        is_global=False,
    )
    # Snapshot the response payload before the request session closes, then run
    # indexing in the background (own session) so the response returns instantly.
    result = DocumentOut.model_validate(document)
    background_tasks.add_task(_run_indexing_bg, document.id)
    return result


@public_router.get("/{doc_id}/file", response_model=None)
def get_document_file(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_query_or_header),
) -> FileResponse | StreamingResponse:
    """Serve a document's original file for inline rendering (authenticated).

    Requires authentication and enforces ownership server-side: the file is served
    only if the document is admin-global OR owned by the requesting user (admins
    may access any). Otherwise a 404 is returned — the same response as a missing
    doc, so existence of another user's private document is never revealed.

    Authentication accepts either an ``Authorization: Bearer`` header (API callers)
    or, when that header is absent, a short-lived access token in a ``?token=``
    query param. The query-param path lets a plain top-level browser navigation
    (opening a source/document link in a new tab) authenticate, since such a
    navigation cannot set request headers. The same ownership check applies in
    both cases, so tenant isolation is unchanged.

    Returns the stored file with the correct media type and an ``inline`` content
    disposition so a browser renders it (and PDF viewers honor ``#page=N`` deep
    links). 404 if the document is missing/deleted or its file is gone from disk.
    """
    document = document_service.get_document(
        db, doc_id, owner_id=_owner_scope(current_user), allow_global=True
    )
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


# --- Admin-only routes ----------------------------------------------------
# Global uploads are admin-only and live on a dedicated, unambiguous literal path
# (``POST /documents/global``) rather than an ``is_global`` flag on the user
# upload route — that keeps the public upload route incapable of ever minting a
# global doc (no client-controlled escalation) and makes the admin intent explicit.
@admin_router.post("/global", response_model=DocumentOut, status_code=201)
async def upload_global_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
) -> DocumentOut:
    """Admin-only: upload a GLOBAL document that grounds every user's answers.

    Sets ``is_global = True`` and ``owner_id = None``. Global docs are searchable
    by everyone but never appear in any user's personal library or sources list.
    """
    contents = await file.read()
    document = document_service.create_document(
        db,
        upload_file_bytes=contents,
        filename=file.filename or "upload",
        uploaded_by=current_user.email,
        owner_id=None,
        is_global=True,
    )
    result = DocumentOut.model_validate(document)
    background_tasks.add_task(_run_indexing_bg, document.id)
    return result


# --- Document library (management) routes ---------------------------------
# Authenticated + tenant-scoped: a user manages ONLY their own docs; an admin
# (resolved via role at request time through ``_owner_scope``) manages all docs.
# Cross-tenant ids 404 identically to missing ids (no IDOR / existence leak).
@public_router.get("", response_model=DocumentList)
def list_documents(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentList:
    """List the caller's own documents (admin: all). Personal library excludes globals.

    DELETED/INACTIVE are excluded unless ``include_inactive`` is set. A regular
    user only ever sees ``owner_id == current_user.id`` rows; admin-global docs are
    searchable but intentionally not shown in the personal library.
    """
    documents = document_service.list_documents(
        db, include_inactive=include_inactive, owner_id=_owner_scope(current_user)
    )
    items = [DocumentOut.model_validate(doc) for doc in documents]
    return DocumentList(items=items, total=len(items))


@public_router.get("/{doc_id}", response_model=DocumentOut)
def get_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentOut:
    """Fetch one of the caller's own documents (404 if missing/deleted/foreign)."""
    document = document_service.get_document_or_404(
        db, doc_id, owner_id=_owner_scope(current_user)
    )
    return DocumentOut.model_validate(document)


@public_router.delete("/{doc_id}", response_model=MessageResponse)
def delete_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """Soft-delete one of the caller's own documents (admin: any) and drop vectors.

    Users cannot delete admin-global docs; a foreign/global id 404s for a user
    (no existence leak). Admins (``_owner_scope`` → None) may delete any document.
    """
    document_service.soft_delete_document(db, doc_id, owner_id=_owner_scope(current_user))
    return MessageResponse(detail=f"Document {doc_id} deleted")


@public_router.post("/{doc_id}/reindex", response_model=ReindexResponse)
def reindex_document(
    doc_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReindexResponse:
    """Reset indexing to pending and queue reindexing (own docs only; admin: any)."""
    document = document_service.set_indexing_status(
        db, doc_id, status="pending", owner_id=_owner_scope(current_user)
    )
    response = ReindexResponse(
        id=document.id,
        indexing_status=document.indexing_status,
        detail="Reindex triggered",
    )
    background_tasks.add_task(_run_indexing_bg, document.id, reindex=True)
    return response
