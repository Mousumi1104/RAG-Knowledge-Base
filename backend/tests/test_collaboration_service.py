"""Self-check for the collaboration-schedule importer + lookup service.

The real schedule (document id 49) lives only in production and is not reachable
from the test environment, so this test builds a *representative fixture PDF*
with the same matrix shape (Room banner spanning workstation columns, a
``WS#n`` header row, and date-keyed data rows) using reportlab, then runs the
actual ``import_collab_schedule`` pdfplumber parsing path end-to-end against it.

It cleans up every ``collaboration_days`` row it inserts (that table is not part
of conftest's per-test TRUNCATE).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete, func, select

from app.database.base import Status
from app.models.collaboration_day import CollaborationDay
from app.services.collaboration_service import (
    collab_day_on,
    next_collab_day,
    normalize_name,
)
from scripts.import_collab_schedule import parse_schedule_pdf, upsert_records

reportlab = pytest.importorskip("reportlab")


def _build_fixture_pdf(path: str) -> None:
    """Write a small schedule PDF whose table mirrors the real doc-49 layout."""
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    from reportlab.lib import colors

    data = [
        # Banner row: Room labels span two workstation columns each.
        ["", "", "Room 11", "", "Room 16", ""],
        # Header row: date/weekday keys + workstation columns.
        ["Date", "Weekday", "WS#9", "WS#10", "WS#11", "WS#12"],
        ["1-Jul-26", "Wed, Working Day", "Mousumi Sengupta", "Arjun Rao", "", "Priya Nair"],
        ["8-Jul-26", "Wed, Working Day", "", "Mousumi Sengupta", "Karan Shah", ""],
        ["15-Jul-26", "Wed, Working Day", "Mousumi Sengupta", "", "Neha Gupta", ""],
    ]
    table = Table(data)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("SPAN", (2, 0), (3, 0)),  # Room 11 over WS#9/WS#10
                ("SPAN", (4, 0), (5, 0)),  # Room 16 over WS#11/WS#12
            ]
        )
    )
    doc = SimpleDocTemplate(path, pagesize=landscape(A4))
    doc.build([table])


@pytest.fixture()
def imported_schedule(db, tmp_path):
    """Parse the fixture PDF, upsert it, yield parsed records, then clean up."""
    pdf_path = str(tmp_path / "collab_schedule_fixture.pdf")
    _build_fixture_pdf(pdf_path)
    records = parse_schedule_pdf(pdf_path)
    upsert_records(db, records)
    try:
        yield records
    finally:
        db.execute(delete(CollaborationDay))
        db.commit()


def test_parser_extracts_expected_records(imported_schedule):
    records = imported_schedule
    mousumi = [
        r for r in records
        if normalize_name(r.employee_name) == normalize_name("Mousumi Sengupta")
    ]
    dates = sorted(r.date for r in mousumi)
    assert dates == [date(2026, 7, 1), date(2026, 7, 8), date(2026, 7, 15)]
    # ws/room reconstructed from header + forward-filled banner.
    first = next(r for r in mousumi if r.date == date(2026, 7, 1))
    assert first.ws == "9"
    assert first.room == "Room 11"
    assert first.weekday == "Wed"


def test_next_collab_day_returns_earliest_upcoming(db, imported_schedule):
    nxt = next_collab_day(db, "Mousumi Sengupta", date(2026, 7, 1))
    assert nxt is not None
    assert nxt.date == date(2026, 7, 1)
    assert nxt.room == "Room 11"
    assert nxt.ws == "9"

    # After the 1st, the next upcoming day is the 8th (loose name match too).
    later = next_collab_day(db, "  mousumi   SENGUPTA ", date(2026, 7, 2))
    assert later is not None
    assert later.date == date(2026, 7, 8)


def test_collab_day_on_and_no_match(db, imported_schedule):
    on = collab_day_on(db, "Mousumi Sengupta", date(2026, 7, 8))
    assert on is not None and on.date == date(2026, 7, 8)
    assert collab_day_on(db, "Mousumi Sengupta", date(2026, 7, 9)) is None
    assert next_collab_day(db, "Nobody Here", date(2026, 7, 1)) is None


def test_upsert_is_idempotent(db, imported_schedule):
    before = db.scalar(select(func.count()).select_from(CollaborationDay))
    # Re-import the same records: no duplicates, count unchanged.
    upsert_records(db, imported_schedule)
    after = db.scalar(select(func.count()).select_from(CollaborationDay))
    assert before == after
