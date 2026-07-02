"""End-to-end tests for the collaboration-day reminder job + employee seeder.

The reminder tables (``employees``, ``collaboration_days``, ``device_tokens``,
``reminders_sent``) are NOT part of conftest's per-test TRUNCATE, so every test
here cleans up the exact rows it inserts in a ``finally`` block.

Covered:
- CHANNEL EXCLUSIVITY: an ACTIVE device token routes to FCM only; without a
  token but with an email, routes to email only.
- DEDUP: the UNIQUE(owner, collab_date) ledger blocks a second send.
- DISABLED/DRY-RUN: with notifications off + no creds, the REAL channels return
  ``{"status": "dry_run"}`` and NO ledger row is written (so it can resend).
- SEEDER: upserts the 22-row sample roster and is idempotent on re-run.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.database.base import Status
from app.models.collaboration_day import CollaborationDay
from app.models.device_token import DeviceToken
from app.models.employee import Employee
from app.models.reminder_sent import ReminderSent
from app.rag.prompt import IST
from scripts import collab_reminder, seed_employees

# A unique identity that will not collide with other tests / leftover rows.
_OWNER = "TESTOWNER_REMINDER"
_NAME = "Reminder Test Person"
_EMAIL = "reminder.test.person@example.com"

_SAMPLE_ROSTER = Path(
    "/tmp/claude-1000/-mnt-d-RAG-Chatbot/"
    "3101a9d4-d6b1-4aa3-a174-b15e23237f1b/scratchpad/employees_seed.json"
)


def _target_date():
    """Tomorrow in IST — the day the reminder job fires the evening before."""
    return datetime.now(IST).date() + timedelta(days=1)


def _cleanup(db) -> None:
    """Remove every row the reminder tests may have created."""
    db.execute(delete(ReminderSent).where(ReminderSent.owner == _OWNER))
    db.execute(delete(DeviceToken).where(DeviceToken.owner == _OWNER))
    db.execute(delete(Employee).where(Employee.employee_id == _OWNER))
    db.execute(delete(CollaborationDay).where(CollaborationDay.employee_name == _NAME))
    db.commit()


@pytest.fixture()
def collab_setup(db):
    """Seed one employee + a matching collab day on the target date; clean up."""
    _cleanup(db)
    db.add(
        Employee(
            employee_id=_OWNER, name=_NAME, email=_EMAIL, status=Status.ACTIVE.value
        )
    )
    db.add(
        CollaborationDay(
            employee_name=_NAME,
            date=_target_date(),
            weekday="Wed",
            ws="9",
            room="Room 11",
            status=Status.ACTIVE.value,
        )
    )
    db.commit()
    try:
        yield db
    finally:
        _cleanup(db)


def _add_active_token(db) -> None:
    db.add(
        DeviceToken(
            owner=_OWNER, token="dtoken-abc", platform="android",
            status=Status.ACTIVE.value,
        )
    )
    db.commit()


def test_channel_exclusivity_fcm_when_token(collab_setup, monkeypatch):
    """An ACTIVE device token -> send_fcm called once, send_email NOT called."""
    db = collab_setup
    _add_active_token(db)

    fcm_calls: list[tuple] = []
    email_calls: list[tuple] = []
    monkeypatch.setattr(
        collab_reminder, "send_fcm",
        lambda *a, **k: fcm_calls.append((a, k)) or {"status": "sent"},
    )
    monkeypatch.setattr(
        collab_reminder, "send_email",
        lambda *a, **k: email_calls.append((a, k)) or {"status": "sent"},
    )

    summary = collab_reminder.main(db)

    assert len(fcm_calls) == 1
    assert len(email_calls) == 0
    assert summary["sent_fcm"] == 1 and summary["sent_email"] == 0
    # FCM was called with the registered token + the collab-reminder data.
    args = fcm_calls[0][0]
    assert args[0] == "dtoken-abc"
    assert args[1] == "Collab Day tomorrow"
    data = args[3]
    assert data["type"] == "collab_reminder"
    assert data["room"] == "Room 11"


def test_channel_exclusivity_email_when_no_token(collab_setup, monkeypatch):
    """No token but an email -> send_email called once, send_fcm NOT called."""
    db = collab_setup  # no device token added

    fcm_calls: list[tuple] = []
    email_calls: list[tuple] = []
    monkeypatch.setattr(
        collab_reminder, "send_fcm",
        lambda *a, **k: fcm_calls.append((a, k)) or {"status": "sent"},
    )
    monkeypatch.setattr(
        collab_reminder, "send_email",
        lambda *a, **k: email_calls.append((a, k)) or {"status": "sent"},
    )

    summary = collab_reminder.main(db)

    assert len(email_calls) == 1
    assert len(fcm_calls) == 0
    assert summary["sent_email"] == 1 and summary["sent_fcm"] == 0
    args = email_calls[0][0]
    assert args[0] == _EMAIL
    assert args[1] == "Reminder: your Collaboration Day is tomorrow"


def test_dedup_second_run_does_not_resend(collab_setup, monkeypatch):
    """The UNIQUE ledger blocks a second send: total sends across two runs == 1."""
    db = collab_setup

    email_calls: list[tuple] = []
    monkeypatch.setattr(
        collab_reminder, "send_fcm",
        lambda *a, **k: {"status": "sent"},
    )
    monkeypatch.setattr(
        collab_reminder, "send_email",
        lambda *a, **k: email_calls.append((a, k)) or {"status": "sent"},
    )

    first = collab_reminder.main(db)
    second = collab_reminder.main(db)

    assert len(email_calls) == 1  # only the first run actually sent
    assert first["sent_email"] == 1
    assert second["sent_email"] == 0 and second["skipped_dedup"] == 1

    # Exactly one ledger row exists for the owner.
    rows = db.execute(
        select(ReminderSent).where(ReminderSent.owner == _OWNER)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].channel == "email"


def test_dry_run_disabled_writes_no_ledger(collab_setup):
    """With notifications off + no creds, real channels dry-run; no ledger row."""
    from app.services import notifications

    db = collab_setup  # has an email -> email channel selected

    # The REAL channels (unpatched) must report dry_run under the default config.
    assert notifications.send_fcm("t", "s", "b", {}) == {"status": "dry_run"}
    assert notifications.send_email("to", "s", "b") == {"status": "dry_run"}

    summary = collab_reminder.main(db)

    assert summary["dry_run"] == 1
    assert summary["sent_email"] == 0 and summary["sent_fcm"] == 0
    # No ledger row written -> the reminder can still fire once creds are enabled.
    rows = db.execute(
        select(ReminderSent).where(ReminderSent.owner == _OWNER)
    ).scalars().all()
    assert rows == []


def test_seed_employees_upserts_and_is_idempotent(db):
    """Seed the 22-row sample roster; re-run updates (idempotent) not inserts."""
    assert _SAMPLE_ROSTER.exists(), f"sample roster missing: {_SAMPLE_ROSTER}"

    import json

    roster = json.loads(_SAMPLE_ROSTER.read_text(encoding="utf-8"))
    ids = [r["employee_id"] for r in roster]
    assert len(ids) == 22
    assert len(set(ids)) == 22  # includes two 'Sneha Mondal' on distinct ids

    # Pre-clean any leftovers from a prior run.
    db.execute(delete(Employee).where(Employee.employee_id.in_(ids)))
    db.commit()

    try:
        inserted, updated = seed_employees.run(str(_SAMPLE_ROSTER))
        assert (inserted, updated) == (22, 0)

        count = len(
            db.execute(
                select(Employee).where(Employee.employee_id.in_(ids))
            ).scalars().all()
        )
        assert count == 22

        # Re-run: fully idempotent (all updates, no new inserts).
        inserted2, updated2 = seed_employees.run(str(_SAMPLE_ROSTER))
        assert (inserted2, updated2) == (0, 22)
        count2 = len(
            db.execute(
                select(Employee).where(Employee.employee_id.in_(ids))
            ).scalars().all()
        )
        assert count2 == 22
    finally:
        db.execute(delete(Employee).where(Employee.employee_id.in_(ids)))
        db.commit()
