"""Daily collaboration-day reminder job (runs the evening BEFORE the collab day).

Cron schedule: 19:00 IST (13:30 UTC). For every ACTIVE employee whose next
scheduled collaboration day is *tomorrow*, send exactly ONE reminder over ONE
mutually-exclusive channel:

- FCM push if the employee has an ACTIVE registered device token, ELSE
- email if the employee has an address on file, ELSE
- skip (no channel).

Idempotency: a ``reminders_sent`` row (UNIQUE(owner, collab_date)) blocks a
second send for the same day. The ledger is written ONLY when a REAL send
occurred (``res["status"] == "sent"``) — a dry-run today (notifications
disabled / no creds) leaves the ledger empty so the reminder can still fire for
real once credentials are enabled.

Run from backend/:

    ./venv/bin/python scripts/collab_reminder.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.base import Status  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models.device_token import DeviceToken  # noqa: E402
from app.models.employee import Employee  # noqa: E402
from app.models.reminder_sent import ReminderSent  # noqa: E402
from app.rag.prompt import IST  # noqa: E402
from app.services import collaboration_service  # noqa: E402
from app.services.notifications import send_email, send_fcm  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

FCM_TITLE = "Collab Day tomorrow"
EMAIL_SUBJECT = "Reminder: your Collaboration Day is tomorrow"


def _room_label(room: str | None) -> str:
    """Return a natural room segment (e.g. ``"Room 11"``) with no doubled word.

    The importer stores the full banner label (``"Room 11"``); if a bare number
    is ever stored we prefix ``"Room "`` so the sentence still reads naturally.
    """
    room = (room or "").strip()
    if not room:
        return "Room"
    if room.casefold().startswith("room"):
        return room
    return f"Room {room}"


def _fcm_date(d) -> str:
    """Render a date as ``"3 Jul 2026"`` (no leading zero on the day)."""
    day = str(int(d.strftime("%d")))
    return d.strftime(f"{day} %b %Y")


def _email_date(d) -> str:
    """Render a date as ``"06 July 2026"`` (leading-zero day, full month)."""
    return d.strftime("%d %B %Y")


def _fcm_body(weekday_full: str, collab_date, room: str | None) -> str:
    """Build the exact FCM body string."""
    return (
        f"You're in office tomorrow ({weekday_full}, {_fcm_date(collab_date)}) "
        f"— {_room_label(room)}."
    )


def _email_body(name: str, weekday_full: str, collab_date, room: str | None) -> str:
    """Build the exact email body (greeted by name) per the spec wording."""
    greeting = f"Dear {name},\n\n" if name else ""
    return (
        f"{greeting}"
        "I hope this message finds you well. I want to take a moment to remind "
        "you all about our upcoming Collaboration Day scheduled for "
        f"{weekday_full} - {_email_date(collab_date)} ({_room_label(room)}). "
        "Please ensure your presence at the office at least 15 minutes before "
        "the scheduled time."
    )


def _reminder_exists(db, owner: str, collab_date) -> bool:
    """True if a ``reminders_sent`` row already exists for (owner, collab_date)."""
    return (
        db.execute(
            select(ReminderSent.id)
            .where(
                ReminderSent.owner == owner,
                ReminderSent.collab_date == collab_date,
            )
            .limit(1)
        ).first()
        is not None
    )


def _active_device_token(db, owner: str) -> DeviceToken | None:
    """Return the owner's ACTIVE device token row, if any."""
    return db.execute(
        select(DeviceToken)
        .where(
            DeviceToken.owner == owner,
            DeviceToken.status == Status.ACTIVE.value,
        )
        .limit(1)
    ).scalars().first()


def main(db) -> dict:
    """Run one reminder pass. Returns the summary counters dict."""
    now = datetime.now(IST)
    today = now.date()
    target = today + timedelta(days=1)  # evening-before => tomorrow's collab day

    summary = {
        "sent_fcm": 0,
        "sent_email": 0,
        "skipped_dedup": 0,
        "dry_run": 0,
        "no_channel": 0,
    }

    employees = db.execute(
        select(Employee).where(Employee.status == Status.ACTIVE.value)
    ).scalars().all()

    logger.info(
        "collab_reminder: today=%s target=%s active_employees=%d",
        today.isoformat(),
        target.isoformat(),
        len(employees),
    )

    for emp in employees:
        # Match the SCHEDULE by the alias when set (distinguishes two same-named
        # people, e.g. "Sneha Mondal" vs "Sneha Mondal (Intern)"); fall back to
        # the display name. The greeting below always uses emp.name (display).
        schedule_name = emp.schedule_name or emp.name
        nxt = collaboration_service.next_collab_day(db, schedule_name, today)
        if nxt is None or nxt.date != target:
            continue

        owner = emp.employee_id
        if _reminder_exists(db, owner, nxt.date):
            summary["skipped_dedup"] += 1
            logger.info(
                "skip (already reminded): %s owner=%s date=%s",
                emp.name,
                owner,
                nxt.date.isoformat(),
            )
            continue

        weekday_full = nxt.date.strftime("%A")

        # Mutually-exclusive channel selection: device token wins over email.
        token_row = _active_device_token(db, owner)
        if token_row is not None:
            channel = "fcm"
            res = send_fcm(
                token_row.token,
                FCM_TITLE,
                _fcm_body(weekday_full, nxt.date, nxt.room),
                {
                    "type": "collab_reminder",
                    "date": nxt.date.isoformat(),
                    "room": nxt.room or "",
                },
            )
        elif emp.email:
            channel = "email"
            res = send_email(
                emp.email,
                EMAIL_SUBJECT,
                _email_body(emp.name, weekday_full, nxt.date, nxt.room),
            )
        else:
            summary["no_channel"] += 1
            logger.info(
                "skip (no channel): %s owner=%s (no device token, no email)",
                emp.name,
                owner,
            )
            continue

        status = res.get("status")
        logger.info(
            "reminder %s owner=%s date=%s channel=%s -> %s",
            emp.name,
            owner,
            nxt.date.isoformat(),
            channel,
            status,
        )

        if status == "sent":
            # Write the dedup ledger ONLY on a real send.
            db.add(
                ReminderSent(
                    owner=owner,
                    collab_date=nxt.date,
                    channel=channel,
                    status=Status.ACTIVE.value,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                # Lost dedup race (another run inserted the same (owner, date)).
                db.rollback()
                summary["skipped_dedup"] += 1
                logger.info(
                    "dedup race lost for owner=%s date=%s",
                    owner,
                    nxt.date.isoformat(),
                )
                continue
            summary["sent_fcm" if channel == "fcm" else "sent_email"] += 1
        elif status == "dry_run":
            # No ledger write, so this can still fire once creds are enabled.
            summary["dry_run"] += 1
        else:
            # An error occurred and was swallowed by the channel; no ledger write.
            logger.error(
                "send error owner=%s date=%s channel=%s detail=%s",
                owner,
                nxt.date.isoformat(),
                channel,
                res.get("detail"),
            )

    logger.info("collab_reminder summary: %s", summary)
    return summary


if __name__ == "__main__":  # pragma: no cover - operational entrypoint
    db = SessionLocal()
    try:
        main(db)
    finally:
        db.close()
