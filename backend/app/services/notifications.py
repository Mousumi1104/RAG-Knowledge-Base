"""Notification channels for the collaboration-day reminder job.

Two mutually-exclusive delivery channels are exposed:

- :func:`send_fcm` — Firebase Cloud Messaging push (for logged-in mobile
  devices that registered a token via ``/device/register``).
- :func:`send_email` — a plain SMTP fallback for employees with no device.

BOTH are DRY-RUN BY DEFAULT. Real sending only happens when
``settings.NOTIFICATIONS_ENABLED`` is True *and* the relevant credentials are
configured. Otherwise the function logs a ``[dry-run]`` line and returns
``{"status": "dry_run"}`` without importing ``firebase-admin`` or opening a
socket — so the module imports cleanly on hosts/CI where those deps or creds are
absent.

Every real-send body is wrapped in ``try/except`` and NEVER raises: a single
failed send must not abort the batch. Return values:

- ``{"status": "dry_run"}``  — gated off (disabled or missing creds).
- ``{"status": "sent"}``     — a real send succeeded.
- ``{"status": "error", "detail": "..."}`` — a real send was attempted but
  failed (logged, swallowed).

Only a ``"sent"`` result should be written to the ``reminders_sent`` dedup
ledger, so a dry-run today can still fire for real once creds are enabled.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Module-level guard so the Firebase app is initialized exactly once per process.
_FIREBASE_APP: Any = None


def _init_firebase() -> Any:
    """Lazily import firebase-admin and initialize the app once.

    Prefers an inline service-account JSON string
    (``FIREBASE_CREDENTIALS_JSON``); falls back to a credentials file path
    (``GOOGLE_APPLICATION_CREDENTIALS``). Returns the initialized app.
    """
    global _FIREBASE_APP
    if _FIREBASE_APP is not None:
        return _FIREBASE_APP

    import firebase_admin
    from firebase_admin import credentials

    if settings.FIREBASE_CREDENTIALS_JSON:
        cred = credentials.Certificate(json.loads(settings.FIREBASE_CREDENTIALS_JSON))
    else:
        # GOOGLE_APPLICATION_CREDENTIALS is a filesystem path to the JSON key.
        cred = credentials.Certificate(settings.GOOGLE_APPLICATION_CREDENTIALS)

    try:
        _FIREBASE_APP = firebase_admin.get_app()
    except ValueError:
        _FIREBASE_APP = firebase_admin.initialize_app(cred)
    return _FIREBASE_APP


def send_fcm(token: str, title: str, body: str, data: dict) -> dict:
    """Send an FCM push notification (dry-run unless enabled + creds present).

    Args:
        token: The recipient device's registration token.
        title: Notification title.
        body: Notification body.
        data: Extra data payload; coerced to all-string values and augmented
            with ``"type": "collab_reminder"``.

    Returns:
        ``{"status": "dry_run"}`` when gated off, ``{"status": "sent"}`` on a
        successful real send, or ``{"status": "error", "detail": ...}`` on a
        swallowed failure. Never raises.
    """
    # data always carries the collab_reminder type + all-string values.
    payload = {"type": "collab_reminder"}
    payload.update({k: str(v) for k, v in (data or {}).items()})

    if not settings.fcm_ready:
        logger.info(
            "[dry-run] fcm title=%r body=%r token=%s data=%s",
            title,
            body,
            (token or "")[:12] + "…" if token else "",
            payload,
        )
        return {"status": "dry_run"}

    try:
        import firebase_admin  # noqa: F401  (ensures the dep is importable)
        from firebase_admin import messaging

        app = _init_firebase()
        message = messaging.Message(
            token=token,
            notification=messaging.Notification(title=title, body=body),
            data=payload,
            android=messaging.AndroidConfig(priority="high"),
        )
        message_id = messaging.send(message, app=app)
        logger.info("fcm sent: id=%s title=%r", message_id, title)
        return {"status": "sent", "id": message_id}
    except Exception as exc:  # noqa: BLE001 - a send failure must not abort the batch
        logger.error("fcm send failed: %s", exc)
        return {"status": "error", "detail": str(exc)}


def send_email(to: str, subject: str, body: str) -> dict:
    """Send a plain-text email over SMTP (dry-run unless enabled + creds present).

    Returns:
        ``{"status": "dry_run"}`` when gated off, ``{"status": "sent"}`` on a
        successful real send, or ``{"status": "error", "detail": ...}`` on a
        swallowed failure. Never raises.
    """
    if not settings.email_ready:
        logger.info(
            "[dry-run] email to=%r subject=%r body=%r", to, subject, body
        )
        return {"status": "dry_run"}

    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = settings.MAIL_FROM
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls()
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
            smtp.send_message(msg)
        logger.info("email sent: to=%r subject=%r", to, subject)
        return {"status": "sent"}
    except Exception as exc:  # noqa: BLE001 - a send failure must not abort the batch
        logger.error("email send failed to=%r: %s", to, exc)
        return {"status": "error", "detail": str(exc)}
