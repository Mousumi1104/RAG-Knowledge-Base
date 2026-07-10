"""Internal, secret-gated operational endpoints.

These routes are NOT part of the public/product API surface. They exist so that
a free external scheduler (GitHub Actions cron) can invoke background jobs over
HTTP — Render's free plan has no cron/Blueprint scheduler. Each route is guarded
by a shared ``X-Cron-Secret`` header compared in constant time against
``settings.CRON_SECRET``.

Currently exposed:
- ``POST /internal/run-collab-reminders`` — run one collaboration-day reminder
  pass (see :mod:`scripts.collab_reminder`). The job already honors
  ``NOTIFICATIONS_ENABLED``, so this endpoint sends NOTHING while notifications
  are disabled; it simply reports the dry-run summary.
- ``POST /internal/test-email`` — send ONE email to a specified address to
  verify email delivery (Brevo HTTP API or SMTP). This EXPLICIT operator test
  bypasses ``NOTIFICATIONS_ENABLED`` but still requires email creds (else 503).
- ``POST /internal/test-push`` — send ONE FCM push to a single owner's active
  device token to verify push delivery. This EXPLICIT operator test bypasses
  ``NOTIFICATIONS_ENABLED`` but still requires Firebase creds (else 503).
"""

from __future__ import annotations

import hmac

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from app.core.config import get_settings
from app.database.base import Status
from app.database.session import SessionLocal
from app.models.device_token import DeviceToken
from app.services import document_service, notifications
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

_HEADER_CRON_SECRET = "X-Cron-Secret"
_HEADER_SERVICE_TOKEN = "X-Service-Token"


def require_service_token(
    x_service_token: str | None = Header(default=None, alias=_HEADER_SERVICE_TOKEN),
) -> None:
    """Gate the handbook-sync route with the shared service token (constant-time).

    - If ``HANDBOOK_SYNC_TOKEN`` is unset/empty the endpoint is DISABLED (503):
      the HR-admin portal can never push a doc without an explicit secret.
    - Otherwise a missing or mismatched ``X-Service-Token`` header -> 401.

    Deliberately a separate secret from ``CRON_SECRET`` (least privilege): the
    admin portal holds only handbook-sync rights, not cron-trigger rights.
    """
    expected = get_settings().HANDBOOK_SYNC_TOKEN
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="handbook sync not configured",
        )
    if not x_service_token or not hmac.compare_digest(x_service_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing service token",
        )


def require_cron_secret(
    x_cron_secret: str | None = Header(default=None, alias=_HEADER_CRON_SECRET),
) -> None:
    """Gate an internal route with the shared cron secret (constant-time compare).

    - If ``CRON_SECRET`` is unset/empty the endpoint is DISABLED (503): the job
      can never be triggered without an explicitly configured secret.
    - Otherwise a missing or mismatched ``X-Cron-Secret`` header -> 401.
    """
    expected = get_settings().CRON_SECRET
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="cron trigger not configured",
        )
    if not x_cron_secret or not hmac.compare_digest(x_cron_secret, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing cron secret",
        )


@router.post("/run-collab-reminders", dependencies=[Depends(require_cron_secret)])
def run_collab_reminders() -> dict:
    """Run one collaboration-day reminder pass and return its summary counters.

    Guarded by :func:`require_cron_secret`. Opens its own DB session, runs
    :func:`scripts.collab_reminder.main`, and returns the summary dict as JSON.
    Honors ``NOTIFICATIONS_ENABLED`` (sends nothing while disabled).
    """
    # Imported lazily so a problem in this ops script can never break web-app
    # startup — only this one endpoint would fail, not the whole service.
    from scripts import collab_reminder

    db = SessionLocal()
    try:
        summary = collab_reminder.main(db)
    finally:
        db.close()
    logger.info("run-collab-reminders triggered: %s", summary)
    return summary


class TestEmailRequest(BaseModel):
    """Body for the operator SMTP delivery test."""

    to: EmailStr
    name: str | None = None


@router.post("/test-email", dependencies=[Depends(require_cron_secret)])
def test_email(req: TestEmailRequest) -> dict:
    """Send ONE test email to ``req.to`` to verify email (Brevo/SMTP) delivery.

    Guarded by :func:`require_cron_secret`. This is an EXPLICIT operator test: it
    bypasses the global ``NOTIFICATIONS_ENABLED`` switch (calling the low-level
    :func:`notifications._send_email_now` directly) but still requires email
    creds — a sender (``MAIL_FROM``) AND at least one transport (``BREVO_API_KEY``
    OR ``SMTP_HOST``) — without them it returns 503. It sends ONLY to the single
    ``to`` address and returns the raw send result JSON.
    """
    if not notifications.email_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="email not configured (need MAIL_FROM + BREVO_API_KEY or SMTP_HOST)",
        )
    result = notifications._send_email_now(
        req.to,
        subject="[TEST] Collab reminder email",
        body=(
            "This is a delivery test for the collaboration-day reminder system. "
            "If you received this, SMTP is working."
        ),
    )
    logger.info("test-email sent to=%r -> %s", req.to, result.get("status"))
    return result


class TestPushRequest(BaseModel):
    """Body for the operator FCM push delivery test.

    ``owner`` optionally targets a specific device-token owner. When omitted, the
    endpoint auto-resolves the target only if EXACTLY ONE active device token
    exists (otherwise it 400s, refusing to guess among several).
    """

    owner: str | None = None


@router.post("/test-push", dependencies=[Depends(require_cron_secret)])
def test_push(req: TestPushRequest) -> dict:
    """Send ONE test FCM push to a single owner's active device token.

    Guarded by :func:`require_cron_secret`. This is an EXPLICIT operator test: it
    bypasses the global ``NOTIFICATIONS_ENABLED`` switch (calling the low-level
    :func:`notifications._fcm_send_now` directly) but still requires Firebase
    creds — without them it returns 503.

    Target resolution:
    - If ``owner`` is given, that owner is used.
    - Else if EXACTLY ONE active device token exists, its owner is used.
    - Otherwise -> 400 ("specify owner").

    The resolved owner must have an ACTIVE ``device_tokens`` row (else 404). The
    push goes to ONLY that single token. Returns the raw send result plus the
    resolved ``owner``.
    """
    db = SessionLocal()
    try:
        owner = req.owner
        if not owner:
            active_owners = list(
                db.execute(
                    select(DeviceToken.owner).where(
                        DeviceToken.status == Status.ACTIVE.value
                    )
                ).scalars()
            )
            if len(active_owners) != 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="specify owner",
                )
            owner = active_owners[0]

        row = db.execute(
            select(DeviceToken).where(
                DeviceToken.owner == owner,
                DeviceToken.status == Status.ACTIVE.value,
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no active device token for owner",
            )

        if not notifications.firebase_configured():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="firebase not configured (need FIREBASE_CREDENTIALS_JSON or GOOGLE_APPLICATION_CREDENTIALS)",
            )

        result = notifications._fcm_send_now(
            row.token,
            title="[TEST] Collab reminder push",
            body="This is a delivery test for the collaboration-day reminder push.",
            data={"test": "1"},
        )
    finally:
        db.close()
    logger.info("test-push sent owner=%r -> %s", owner, result.get("status"))
    return {**result, "owner": owner}


@router.post("/sync-global-doc", dependencies=[Depends(require_service_token)])
async def sync_global_doc(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    ref: str = Form("hr_handbook"),
) -> dict:
    """Replace-by-ref upsert of a synced admin-global document.

    The server-to-server bridge used by the HR-admin portal
    (``application/controllers/Handbook.php``): whenever HR updates the HR
    Handbook, the portal POSTs the PDF here so the RAG global knowledge base —
    which grounds every Employee-Zone chatbot answer — stays in sync.

    Guarded by :func:`require_service_token`. "Replace old" semantics: every
    ACTIVE global doc already tagged with ``ref`` is soft-deleted (its vectors
    dropped) BEFORE the new upload is created and indexed, so exactly one live
    copy of the handbook exists and duplicate-PDF answers can't accumulate. The
    new doc is created ``is_global=True, owner_id=None, external_ref=ref`` and
    indexed in the background. Returns the replaced ids and the new doc id.
    """
    contents = await file.read()

    db = SessionLocal()
    try:
        replaced = document_service.list_active_globals_by_ref(db, ref)
        replaced_ids = [doc.id for doc in replaced]
        for doc in replaced:
            document_service.soft_delete_document(db, doc.id, owner_id=None)

        document = document_service.create_document(
            db,
            upload_file_bytes=contents,
            filename=file.filename or "handbook.pdf",
            uploaded_by="hr-admin-portal",
            owner_id=None,
            is_global=True,
            external_ref=ref,
        )
        new_id = document.id
    finally:
        db.close()

    # Index AFTER the response is sent, in a fresh session (see documents.py).
    from app.api.documents import _run_indexing_bg

    background_tasks.add_task(_run_indexing_bg, new_id)
    logger.info("sync-global-doc ref=%r new_id=%s replaced=%s", ref, new_id, replaced_ids)
    return {"status": "ok", "ref": ref, "id": new_id, "replaced": replaced_ids}
