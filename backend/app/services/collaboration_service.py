"""Lookup helpers over the ``collaboration_days`` schedule table.

The importer (:mod:`scripts.import_collab_schedule`) populates
``collaboration_days`` from the schedule PDF; this module exposes the read-side
used by the reminder job: given an employee's display name and a reference date,
find their next scheduled on-site collaboration day.

Names in the schedule are matched *loosely* — the PDF and the caller may differ
in casing and internal spacing — so both sides normalize through
:func:`normalize_name` before comparison. The comparison is pushed into SQL (a
``lower``/whitespace-collapsed match) so the ``(employee_name, date)`` index
still orders the candidates and we only materialise the earliest row.
"""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import ColumnElement, func, select

from app.database.base import Status
from app.models.collaboration_day import CollaborationDay

# Collapse any run of whitespace to a single space (matches the SQL side below).
_WS_RE = re.compile(r"\s+")


def normalize_name(name: str | None) -> str:
    """Return a canonical form of ``name`` for loose equality matching.

    Casefolds, trims, and collapses internal whitespace runs to a single space
    so that ``"  Mousumi   Sengupta "`` and ``"mousumi sengupta"`` compare equal.
    Shared by the importer (when it canonicalises stored names) and by the
    lookups here, so both sides agree on what "the same person" means.
    """
    return _WS_RE.sub(" ", (name or "")).strip().casefold()


def _normalized_name_col() -> ColumnElement[str]:
    """SQL expression mirroring :func:`normalize_name` for ``employee_name``.

    ``regexp_replace(..., '\\s+', ' ', 'g')`` collapses internal whitespace,
    ``lower`` mirrors ``casefold`` (equivalent for the ASCII names in the
    schedule), and ``trim`` drops the leading/trailing space. Kept in lock-step
    with the Python normaliser so the comparison never silently diverges.
    """
    return func.trim(
        func.regexp_replace(func.lower(CollaborationDay.employee_name), r"\s+", " ", "g")
    )


def next_collab_day(
    db, employee_name: str, today: date
) -> CollaborationDay | None:
    """Return the employee's earliest ACTIVE collab day on/after ``today``.

    Args:
        db: An open SQLAlchemy session.
        employee_name: The employee's display name (matched case/space-loosely).
        today: The reference date; days strictly before it are ignored, the
            day itself counts as upcoming.

    Returns:
        The single earliest matching :class:`CollaborationDay`, or ``None`` when
        the employee has no upcoming scheduled day.
    """
    target = normalize_name(employee_name)
    if not target:
        return None
    stmt = (
        select(CollaborationDay)
        .where(
            CollaborationDay.status == Status.ACTIVE.value,
            CollaborationDay.date >= today,
            _normalized_name_col() == target,
        )
        .order_by(CollaborationDay.date.asc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def collab_day_on(
    db, employee_name: str, target_date: date
) -> CollaborationDay | None:
    """Return the employee's ACTIVE collab day on ``target_date`` if any."""
    target = normalize_name(employee_name)
    if not target:
        return None
    stmt = (
        select(CollaborationDay)
        .where(
            CollaborationDay.status == Status.ACTIVE.value,
            CollaborationDay.date == target_date,
            _normalized_name_col() == target,
        )
        .order_by(CollaborationDay.date.asc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()
