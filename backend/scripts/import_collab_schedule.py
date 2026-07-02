"""Populate ``collaboration_days`` from the Collaboration Schedule PDF (doc 49).

The schedule PDF is a matrix: banner rows carry Room labels that span several
workstation columns, a header row names the workstations (``WS#9`` ...), and each
data row is keyed by a date (``1-Jul-26``) plus its weekday, with the value cells
holding the employee sitting at each workstation that day. We reconstruct that
matrix the same way :func:`app.services.extraction._linearize_table` does —
forward-fill the Room banner across columns, read the workstation header, then
walk the data rows — but instead of emitting text we emit one structured
:class:`CollabRecord` per non-empty value cell.

Design choices (documented per the task):
- ``ws`` is stored as the bare workstation number: ``WS#9`` -> ``"9"``.
- ``room`` KEEPS the full banner label (``"Room 11"``) rather than just ``"11"``:
  it reads naturally in a reminder message ("your seat is in Room 11") and the
  column is wide enough (``String(64)``).
- ``weekday`` stores the short day token (``"Wed"``) extracted from the
  ``Wed, Working Day`` cell.

The upsert is idempotent, keyed by (canonicalised ``employee_name``, ``date``):
re-running updates ws/room/weekday and re-activates the row instead of inserting
a duplicate. Parse failures on a row/cell are logged and skipped — the importer
never aborts mid-file.

Run from backend/:

    ./venv/bin/python scripts/import_collab_schedule.py            # doc id 49
    ./venv/bin/python scripts/import_collab_schedule.py 49 --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.base import Status  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models.collaboration_day import CollaborationDay  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.services.collaboration_service import normalize_name  # noqa: E402
from app.services.extraction import _local_path  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

DEFAULT_DOC_ID = 49
# Title used if the numeric id lookup misses (e.g. a re-uploaded schedule).
_TITLE_LIKE = "Collaboration_Days_Schedule%Rooms_Updated"

# A date cell like "1-Jul-26" (also tolerates a 4-digit year and full month name).
_DATE_RE = re.compile(r"\b(\d{1,2}-[A-Za-z]{3,}-\d{2,4})\b")
# A weekday token; only the abbreviation is captured/stored ("Wed").
_WEEKDAY_RE = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)", re.IGNORECASE)
# A workstation number inside a header cell like "WS#9" / "WS 9".
_WS_NUM_RE = re.compile(r"(\d+)")
# Collapse internal whitespace in a stored display name (dedup-stable).
_WS_RE_COLLAPSE = re.compile(r"\s+")

# Value-cell placeholders that mark an empty seat rather than a person.
_NON_NAMES = {"", "-", "--", "—", "n/a", "na", "tbd", "vacant", "empty", "holiday"}


@dataclass(frozen=True)
class CollabRecord:
    """One employee scheduled at one workstation on one day."""

    employee_name: str
    date: date
    weekday: str | None
    ws: str | None
    room: str | None


def _parse_date(cell: str) -> date | None:
    """Extract and parse the first ``d-Mon-yy`` date found in ``cell``."""
    m = _DATE_RE.search(cell)
    if not m:
        return None
    token = m.group(1)
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d-%B-%y", "%d-%B-%Y"):
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    return None


def _parse_weekday(*cells: str) -> str | None:
    """Return the short weekday token (``"Wed"``) found across ``cells``."""
    for cell in cells:
        m = _WEEKDAY_RE.search(cell)
        if m:
            return m.group(1).title()
    return None


def _parse_ws(header_cell: str) -> str | None:
    """``"WS#9"`` -> ``"9"``; fall back to the trimmed header if no number."""
    m = _WS_NUM_RE.search(header_cell)
    if m:
        return m.group(1)
    cell = header_cell.strip()
    return cell or None


def parse_schedule_tables(tables: list[list[list[str | None]]]) -> list[CollabRecord]:
    """Reconstruct the schedule matrix from extracted table grids.

    Mirrors the banner forward-fill + header detection of
    :func:`app.services.extraction._linearize_table`, but yields structured
    :class:`CollabRecord` rows. Robust to rows/cells it cannot parse: they are
    logged and skipped.
    """
    records: list[CollabRecord] = []
    for table in tables:
        rows = [
            [(c or "").strip() for c in row]
            for row in table
            if any((c or "").strip() for c in row)
        ]
        if len(rows) < 2:
            continue
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]

        # Header = the row with the most non-empty cells (earliest wins on a tie).
        counts = [sum(1 for c in r if c) for r in rows]
        h = max(range(len(rows)), key=lambda i: counts[i])
        header = rows[h]

        # Banner rows above the header become forward-filled per-column Room
        # labels (single-value banner rows are treated as captions and ignored).
        group = [""] * ncols
        for r in rows[:h]:
            non_empty = [c for c in r if c]
            if len(non_empty) < 2:
                continue
            last = ""
            for i in range(ncols):
                if r[i]:
                    last = r[i]
                if last:
                    group[i] = last

        # Key columns = the leading run sharing column 0's group label; the first
        # column with a different (Room) group starts the value columns.
        key_end = ncols
        for i in range(ncols):
            if group[i] != group[0]:
                key_end = i
                break
        # No banner at all: assume the leading date (+ weekday) columns are keys
        # and everything from the first workstation header onward is a value col.
        if key_end == ncols:
            key_end = 0
            for i in range(ncols):
                if header[i] and _WS_NUM_RE.search(header[i]):
                    key_end = i
                    break

        for r in rows[h + 1:]:
            if not any(r):
                continue
            key_cells = [r[i] for i in range(key_end)]
            row_date = next(
                (d for d in (_parse_date(c) for c in key_cells) if d), None
            )
            if row_date is None:
                # Not a data row (sub-header, blank, notes) — skip quietly.
                if any(key_cells):
                    logger.debug("Skipping non-date schedule row: %r", key_cells)
                continue
            weekday = _parse_weekday(*key_cells)

            for i in range(key_end, ncols):
                cell = r[i]
                if cell.casefold() in _NON_NAMES:
                    continue
                records.append(
                    CollabRecord(
                        employee_name=_WS_RE_COLLAPSE.sub(" ", cell).strip(),
                        date=row_date,
                        weekday=weekday,
                        ws=_parse_ws(header[i]) if i < len(header) else None,
                        room=(group[i] or None),
                    )
                )
    return records


def parse_schedule_pdf(local_path: str) -> list[CollabRecord]:
    """Open ``local_path`` with pdfplumber and parse every page's tables."""
    import pdfplumber

    all_tables: list[list[list[str | None]]] = []
    with pdfplumber.open(local_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            try:
                tables = page.extract_tables() or []
            except Exception as exc:  # noqa: BLE001 - skip this page's tables
                logger.warning("Table extraction failed on page %d: %s", page_number, exc)
                continue
            all_tables.extend(tables)
    return parse_schedule_tables(all_tables)


def resolve_document(db, doc_id: int = DEFAULT_DOC_ID) -> Document | None:
    """Find the schedule document by id, then by title as a fallback."""
    doc = db.get(Document, doc_id)
    if doc is not None and doc.status != Status.DELETED.value:
        return doc
    return db.execute(
        select(Document)
        .where(
            Document.filename.ilike(_TITLE_LIKE),
            Document.status != Status.DELETED.value,
        )
        .order_by(Document.id.desc())
        .limit(1)
    ).scalars().first()


def upsert_records(db, records: list[CollabRecord]) -> tuple[int, int]:
    """Idempotently upsert ``records`` keyed by (employee_name, date).

    Returns ``(inserted, updated)``. An existing non-deleted row for the same
    (name, date) is updated in place and re-activated; otherwise a new row is
    inserted. Commits once at the end.
    """
    inserted = updated = 0
    for rec in records:
        existing = db.execute(
            select(CollaborationDay)
            .where(
                CollaborationDay.employee_name == rec.employee_name,
                CollaborationDay.date == rec.date,
                CollaborationDay.status != Status.DELETED.value,
            )
            .limit(1)
        ).scalars().first()
        if existing is None:
            db.add(
                CollaborationDay(
                    employee_name=rec.employee_name,
                    date=rec.date,
                    weekday=rec.weekday,
                    ws=rec.ws,
                    room=rec.room,
                    status=Status.ACTIVE.value,
                )
            )
            inserted += 1
        else:
            existing.weekday = rec.weekday
            existing.ws = rec.ws
            existing.room = rec.room
            existing.status = Status.ACTIVE.value
            updated += 1
    db.commit()
    return inserted, updated


def _summarize(records: list[CollabRecord]) -> str:
    """Human-readable summary: row count, distinct names, date range."""
    if not records:
        return "no records parsed"
    names = {normalize_name(r.employee_name) for r in records}
    dates = sorted({r.date for r in records})
    return (
        f"{len(records)} rows, {len(names)} distinct employees, "
        f"dates {dates[0].isoformat()}..{dates[-1].isoformat()}"
    )


def run(doc_id: int = DEFAULT_DOC_ID, dry_run: bool = False) -> int:
    """Resolve the schedule doc, parse it, and upsert. Returns rows written."""
    db = SessionLocal()
    try:
        doc = resolve_document(db, doc_id)
        if doc is None:
            logger.error("Schedule document %s not found (and no title match)", doc_id)
            return 0
        logger.info("Schedule document %s: %s", doc.id, doc.filename)
        with _local_path(doc.file_path, doc.document_type) as local:
            records = parse_schedule_pdf(local)
        logger.info("Parsed schedule: %s", _summarize(records))
        if dry_run:
            logger.info("Dry-run: not writing %d records", len(records))
            return 0
        inserted, updated = upsert_records(db, records)
        logger.info(
            "Imported collaboration_days: %d inserted, %d updated (%s)",
            inserted,
            updated,
            _summarize(records),
        )
        return inserted + updated
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the collaboration schedule PDF.")
    parser.add_argument("doc_id", nargs="?", type=int, default=DEFAULT_DOC_ID)
    parser.add_argument("--dry-run", action="store_true", help="parse and log, do not write")
    args = parser.parse_args()
    run(doc_id=args.doc_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
