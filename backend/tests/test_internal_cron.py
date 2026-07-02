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

    monkeypatch.setattr(internal.collab_reminder, "main", _fake_main)
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
