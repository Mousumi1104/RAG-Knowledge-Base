"""Idempotent seeder for the ``employees`` directory table.

Reads a JSON roster file — a list of ``{"employee_id", "name", "email"}``
objects — and upserts each row keyed by the UNIQUE ``employee_id`` (name/email
are updated in place on re-run, and a soft-deleted row is re-activated).

The roster is NEVER hardcoded here: the real data file is supplied at deploy
time. The path is resolved from (in order): ``argv[1]``, the
``EMPLOYEES_SEED_JSON`` env var, else a repo-local default
(``backend/data/employees.json``).

Run from backend/:

    ./venv/bin/python scripts/seed_employees.py /path/to/roster.json
    EMPLOYEES_SEED_JSON=/path/to/roster.json ./venv/bin/python scripts/seed_employees.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.base import Status  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models.employee import Employee  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

# Repo-local default (uncommitted real roster is dropped here at deploy time).
_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "employees.json"


def _resolve_path(argv_path: str | None) -> Path:
    """Pick the roster path: argv > env > repo default."""
    if argv_path:
        return Path(argv_path)
    env_path = os.getenv("EMPLOYEES_SEED_JSON")
    if env_path:
        return Path(env_path)
    return _DEFAULT_PATH


def load_roster(path: Path) -> list[dict]:
    """Read and validate the roster JSON (a list of employee objects)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"roster must be a JSON list, got {type(data).__name__}")
    return data


def seed_employees(db, roster: list[dict]) -> tuple[int, int]:
    """Upsert ``roster`` into ``employees`` keyed by ``employee_id`` UNIQUE.

    Returns ``(inserted, updated)``. Rows missing ``employee_id`` or ``name`` are
    logged and skipped. Commits once at the end.
    """
    inserted = updated = 0
    for row in roster:
        employee_id = (row.get("employee_id") or "").strip()
        name = (row.get("name") or "").strip()
        email = (row.get("email") or "").strip() or None
        if not employee_id or not name:
            logger.warning("Skipping roster row (missing employee_id/name): %r", row)
            continue

        existing = db.execute(
            select(Employee).where(Employee.employee_id == employee_id).limit(1)
        ).scalars().first()

        if existing is None:
            db.add(
                Employee(
                    employee_id=employee_id,
                    name=name,
                    email=email,
                    status=Status.ACTIVE.value,
                )
            )
            inserted += 1
        else:
            existing.name = name
            existing.email = email
            existing.status = Status.ACTIVE.value
            updated += 1

    db.commit()
    return inserted, updated


def run(argv_path: str | None = None) -> tuple[int, int]:
    """Resolve the roster path, load it, and upsert. Returns (inserted, updated)."""
    path = _resolve_path(argv_path)
    if not path.exists():
        logger.error("Roster file not found: %s", path)
        return (0, 0)
    roster = load_roster(path)
    db = SessionLocal()
    try:
        inserted, updated = seed_employees(db, roster)
        logger.info(
            "seed_employees: %d inserted, %d updated (from %s, %d rows)",
            inserted,
            updated,
            path,
            len(roster),
        )
        return inserted, updated
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover - operational entrypoint
    run(sys.argv[1] if len(sys.argv) > 1 else None)
