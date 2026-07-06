"""Notification channels for the collaboration-day reminder job.

Two mutually-exclusive delivery channels are exposed:

- :func:`send_fcm` — Firebase Cloud Messaging push (for logged-in mobile
  devices that registered a token via ``/employee/device/register``).
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


def email_configured() -> bool:
    """True when a sender + at least one transport (Brevo OR SMTP) are present.

    Independent of ``NOTIFICATIONS_ENABLED`` — this only asks "could we deliver
    an email at all?", which the operator test-email endpoint uses to decide
    between 503 (unconfigured) and an actual send attempt. Delegates to the
    :attr:`Settings.email_configured` property.
    """
    return bool(settings.email_configured)


def _brevo_send(to: str, subject: str, body: str) -> dict:
    """Low-level send via Brevo's HTTP transactional API (never gated).

    Used where outbound SMTP is blocked (e.g. Render's free tier). Assumes
    ``BREVO_API_KEY`` + ``MAIL_FROM`` are present (callers check). Brevo returns
    HTTP 201 on success.

    Returns:
        ``{"status": "sent", "provider": "brevo"}`` on a 2xx, or
        ``{"status": "error", "detail": ...}`` on a non-2xx / exception. Never
        raises.
    """
    try:
        import httpx

        resp = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": settings.BREVO_API_KEY or "",
                "accept": "application/json",
                "content-type": "application/json",
            },
            json={
                "sender": {"email": settings.MAIL_FROM},
                "to": [{"email": to}],
                "subject": subject,
                "textContent": body,
            },
            timeout=30.0,
        )
        if 200 <= resp.status_code < 300:
            logger.info("email sent via brevo: to=%r subject=%r", to, subject)
            return {"status": "sent", "provider": "brevo"}
        detail = f"{resp.status_code}: {resp.text[:300]}"
        logger.error("brevo send failed to=%r: %s", to, detail)
        return {"status": "error", "detail": detail}
    except Exception as exc:  # noqa: BLE001 - a send failure must not abort the batch
        logger.error("brevo send raised to=%r: %s", to, exc)
        return {"status": "error", "detail": str(exc)}


def _smtp_send(to: str, subject: str, body: str) -> dict:
    """Low-level SMTP send: performs the actual smtplib delivery, never gated.

    This does NOT check ``NOTIFICATIONS_ENABLED`` — it is the raw send used both
    by :func:`send_email` (after its dry-run guard passes) and by the operator
    test-email endpoint. Assumes SMTP creds are present (callers check).

    Returns:
        ``{"status": "sent"}`` on success, or ``{"status": "error",
        "detail": ...}`` on a swallowed failure. Never raises.
    """
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


def _send_email_now(to: str, subject: str, body: str) -> dict:
    """Really send an email, choosing a transport (never gated on enablement).

    Prefers Brevo's HTTP API when ``BREVO_API_KEY`` is set (works where SMTP is
    firewalled); otherwise falls back to raw SMTP when ``SMTP_HOST`` is set. Used
    both by :func:`send_email` (after its dry-run guard passes) and directly by
    the operator test-email endpoint.

    Returns:
        the transport's ``{"status": "sent", ...}`` / ``{"status": "error",
        "detail": ...}`` dict, or ``{"status": "error", "detail": "no email
        transport configured"}`` when neither transport is set. Never raises.
    """
    if settings.BREVO_API_KEY:
        return _brevo_send(to, subject, body)
    if settings.SMTP_HOST:
        return _smtp_send(to, subject, body)
    return {"status": "error", "detail": "no email transport configured"}


def send_email(to: str, subject: str, body: str) -> dict:
    """Send a plain-text email (dry-run unless enabled + creds present).

    Delivery uses Brevo's HTTP API when configured, else SMTP (see
    :func:`_send_email_now`).

    Returns:
        ``{"status": "dry_run"}`` when gated off, ``{"status": "sent", ...}`` on
        a successful real send, or ``{"status": "error", "detail": ...}`` on a
        swallowed failure. Never raises.
    """
    if not settings.email_ready:
        logger.info(
            "[dry-run] email to=%r subject=%r body=%r", to, subject, body
        )
        return {"status": "dry_run"}

    return _send_email_now(to, subject, body)
