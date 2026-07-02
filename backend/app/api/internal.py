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
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.utils.logging import get_logger
from scripts import collab_reminder

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
    db = SessionLocal()
    try:
        summary = collab_reminder.main(db)
    finally:
        db.close()
    logger.info("run-collab-reminders triggered: %s", summary)
    return summary
