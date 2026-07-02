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
  verify SMTP delivery. This EXPLICIT operator test bypasses
  ``NOTIFICATIONS_ENABLED`` but still requires SMTP creds (else 503).
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.services import notifications
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

_HEADER_CRON_SECRET = "X-Cron-Secret"


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
    """Send ONE test email to ``req.to`` to verify SMTP (Brevo) delivery.

    Guarded by :func:`require_cron_secret`. This is an EXPLICIT operator test: it
    bypasses the global ``NOTIFICATIONS_ENABLED`` switch (calling the low-level
    :func:`notifications._smtp_send` directly) but still requires SMTP creds
    (``SMTP_HOST`` + ``MAIL_FROM``) — without them it returns 503. It sends ONLY
    to the single ``to`` address and returns the raw send result JSON.
    """
    if not notifications.smtp_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SMTP not configured (need SMTP_HOST + MAIL_FROM)",
        )
    result = notifications._smtp_send(
        req.to,
        subject="[TEST] Collab reminder email",
        body=(
            "This is a delivery test for the collaboration-day reminder system. "
            "If you received this, SMTP is working."
        ),
    )
    logger.info("test-email sent to=%r -> %s", req.to, result.get("status"))
    return result
