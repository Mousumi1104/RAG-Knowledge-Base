"""Config hardening tests: an empty bool env var must never crash boot.

A stray ``SMTP_USE_TLS=`` (empty string) in the environment previously raised a
``ValidationError`` at import time and took the whole service down on Render. A
``mode="before"`` field validator now falls back to the field default for an
empty/whitespace (or ``None``) value, while still coercing real truthy/falsy
strings normally.

These construct ``Settings(_env_file=None)`` directly (bypassing the ``.env``
file and the ``get_settings`` lru_cache) so the assertions read ONLY the env we
set via ``monkeypatch``.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def test_empty_smtp_use_tls_falls_back_to_default(monkeypatch):
    """SMTP_USE_TLS="" must not raise and must equal the field default (True)."""
    monkeypatch.setenv("SMTP_USE_TLS", "")

    settings = Settings(_env_file=None)  # must NOT raise

    assert settings.SMTP_USE_TLS is True  # the declared default


def test_whitespace_smtp_use_tls_falls_back_to_default(monkeypatch):
    """A whitespace-only value is treated as empty -> default."""
    monkeypatch.setenv("SMTP_USE_TLS", "   ")

    settings = Settings(_env_file=None)

    assert settings.SMTP_USE_TLS is True


def test_empty_notifications_enabled_falls_back_to_default(monkeypatch):
    """NOTIFICATIONS_ENABLED="" must not raise and must equal its default (False)."""
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "")

    settings = Settings(_env_file=None)

    assert settings.NOTIFICATIONS_ENABLED is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("false", False), ("0", False), ("true", True), ("1", True)],
)
def test_real_bool_strings_still_coerced(monkeypatch, raw, expected):
    """Non-empty values keep pydantic's normal bool coercion."""
    monkeypatch.setenv("SMTP_USE_TLS", raw)

    settings = Settings(_env_file=None)

    assert settings.SMTP_USE_TLS is expected


def test_email_configured_requires_sender_and_a_transport(monkeypatch):
    """email_configured: MAIL_FROM AND (BREVO_API_KEY OR SMTP_HOST)."""
    for var in ("MAIL_FROM", "BREVO_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(var, raising=False)

    # No sender, no transport.
    assert Settings(_env_file=None).email_configured is False

    # Sender but no transport.
    monkeypatch.setenv("MAIL_FROM", "noreply@example.com")
    assert Settings(_env_file=None).email_configured is False

    # Sender + Brevo transport.
    monkeypatch.setenv("BREVO_API_KEY", "k")
    assert Settings(_env_file=None).email_configured is True

    # Sender + SMTP transport (drop Brevo).
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    assert Settings(_env_file=None).email_configured is True
