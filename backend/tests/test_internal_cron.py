"""Tests for the secret-gated internal cron-trigger endpoint.

``POST /internal/run-collab-reminders`` runs the collab-reminder job when
presented with a matching ``X-Cron-Secret``. We monkeypatch
``collab_reminder.main`` to a stub so the endpoint never touches external state
(no FCM/SMTP, no ledger writes) and we control the returned summary, and we
monkeypatch ``get_settings`` inside the router to flip ``CRON_SECRET`` on/off.

Gate matrix:
- CRON_SECRET set + correct header -> 200, returns the stub summary
- wrong header                     -> 401
- missing header                   -> 401
- CRON_SECRET unset (default)      -> 503
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.api import internal
from scripts import collab_reminder

_ENDPOINT = "/internal/run-collab-reminders"
_SECRET = "s3cr3t-cron-token"

_FAKE_SUMMARY = {
    "sent_fcm": 0,
    "sent_email": 0,
    "skipped_dedup": 0,
    "dry_run": 3,
    "no_channel": 1,
}


@dataclass
class _FakeSettings:
    """Minimal stand-in exposing just the attribute the gate reads."""

    CRON_SECRET: str | None


@pytest.fixture()
def stub_job(monkeypatch):
    """Replace collab_reminder.main with a stub that records it was called."""
    calls: list[object] = []

    def _fake_main(db) -> dict:
        calls.append(db)
        return dict(_FAKE_SUMMARY)

    # The route lazily does ``from scripts import collab_reminder`` and calls
    # ``.main`` on it, so patch the real module (not an ``internal`` attribute,
    # which the lazy import never binds at module scope).
    monkeypatch.setattr(collab_reminder, "main", _fake_main)
    return calls


def _set_secret(monkeypatch, value: str | None) -> None:
    """Force the router's view of CRON_SECRET without touching the real cache."""
    monkeypatch.setattr(internal, "get_settings", lambda: _FakeSettings(CRON_SECRET=value))


def test_correct_secret_runs_job_and_returns_summary(anon_client, monkeypatch, stub_job):
    _set_secret(monkeypatch, _SECRET)

    resp = anon_client.post(_ENDPOINT, headers={"X-Cron-Secret": _SECRET})

    assert resp.status_code == 200
    assert resp.json() == _FAKE_SUMMARY
    assert len(stub_job) == 1  # the job ran exactly once


def test_wrong_secret_is_unauthorized(anon_client, monkeypatch, stub_job):
    _set_secret(monkeypatch, _SECRET)

    resp = anon_client.post(_ENDPOINT, headers={"X-Cron-Secret": "nope"})

    assert resp.status_code == 401
    assert stub_job == []  # job never ran


def test_missing_header_is_unauthorized(anon_client, monkeypatch, stub_job):
    _set_secret(monkeypatch, _SECRET)

    resp = anon_client.post(_ENDPOINT)

    assert resp.status_code == 401
    assert stub_job == []


def test_unconfigured_secret_is_service_unavailable(anon_client, monkeypatch, stub_job):
    _set_secret(monkeypatch, None)

    resp = anon_client.post(_ENDPOINT, headers={"X-Cron-Secret": _SECRET})

    assert resp.status_code == 503
    assert stub_job == []


# ---------------------------------------------------------------------------
# POST /internal/test-email — explicit operator SMTP delivery test.
# ---------------------------------------------------------------------------
_TEST_EMAIL_ENDPOINT = "/internal/test-email"
_TO = "ops.check@example.com"


def _set_smtp(monkeypatch, host, mail_from) -> None:
    """Force the notifications module's view of the SMTP creds."""
    from app.services import notifications

    monkeypatch.setattr(notifications.settings, "SMTP_HOST", host)
    monkeypatch.setattr(notifications.settings, "MAIL_FROM", mail_from)


def test_test_email_sends_once_with_creds_and_secret(anon_client, monkeypatch):
    """Creds present + correct secret -> 200 and _smtp_send called once with `to`."""
    from app.services import notifications

    _set_secret(monkeypatch, _SECRET)
    _set_smtp(monkeypatch, "smtp.example.com", "noreply@example.com")

    calls: list[tuple] = []
    monkeypatch.setattr(
        notifications, "_smtp_send",
        lambda to, subject, body: calls.append((to, subject, body)) or {"status": "sent"},
    )

    resp = anon_client.post(
        _TEST_EMAIL_ENDPOINT,
        headers={"X-Cron-Secret": _SECRET},
        json={"to": _TO, "name": "Ops"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "sent"}
    assert len(calls) == 1
    assert calls[0][0] == _TO  # sent ONLY to the given address


def test_test_email_wrong_secret_is_unauthorized(anon_client, monkeypatch):
    from app.services import notifications

    _set_secret(monkeypatch, _SECRET)
    _set_smtp(monkeypatch, "smtp.example.com", "noreply@example.com")

    calls: list[tuple] = []
    monkeypatch.setattr(
        notifications, "_smtp_send",
        lambda *a, **k: calls.append((a, k)) or {"status": "sent"},
    )

    resp = anon_client.post(
        _TEST_EMAIL_ENDPOINT,
        headers={"X-Cron-Secret": "nope"},
        json={"to": _TO},
    )

    assert resp.status_code == 401
    assert calls == []  # never sent


def test_test_email_missing_secret_is_unauthorized(anon_client, monkeypatch):
    from app.services import notifications

    _set_secret(monkeypatch, _SECRET)
    _set_smtp(monkeypatch, "smtp.example.com", "noreply@example.com")

    calls: list[tuple] = []
    monkeypatch.setattr(
        notifications, "_smtp_send",
        lambda *a, **k: calls.append((a, k)) or {"status": "sent"},
    )

    resp = anon_client.post(_TEST_EMAIL_ENDPOINT, json={"to": _TO})

    assert resp.status_code == 401
    assert calls == []


def test_test_email_missing_smtp_creds_is_service_unavailable(anon_client, monkeypatch):
    """SMTP_HOST/MAIL_FROM absent -> 503, and no send attempted."""
    from app.services import notifications

    _set_secret(monkeypatch, _SECRET)
    _set_smtp(monkeypatch, None, None)

    calls: list[tuple] = []
    monkeypatch.setattr(
        notifications, "_smtp_send",
        lambda *a, **k: calls.append((a, k)) or {"status": "sent"},
    )

    resp = anon_client.post(
        _TEST_EMAIL_ENDPOINT,
        headers={"X-Cron-Secret": _SECRET},
        json={"to": _TO},
    )

    assert resp.status_code == 503
    assert calls == []  # creds check short-circuits before any send


def test_reminder_job_dry_run_path_unaffected():
    """The reminder job's real channels still dry-run under the default config.

    The test-email endpoint bypasses NOTIFICATIONS_ENABLED, but the standard
    send_email() path must remain gated (dry-run) when notifications are off.
    """
    from app.services import notifications

    assert notifications.send_email("to@example.com", "s", "b") == {"status": "dry_run"}
