"""Tests for the Employee-Zone device-token + logout API.

Exercises ``POST /employee/device/register`` / ``/employee/device/unregister`` /
``/employee/logout`` against
the local providers + Postgres test DB from ``conftest.py``. Identity is the
Mode-A owner key (``X-Employee-Id`` header, falling back to the resolved email) --
the SAME key the reminder job and chat-history stack use.

The ``device_tokens`` / ``employees`` tables are NOT in conftest's per-test
TRUNCATE (only ``chunks`` / ``documents``), so every test uses a UNIQUE owner and
hard-deletes the rows it creates in a ``finally``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from app.core.config import get_settings
from app.database.base import Status
from app.database.session import SessionLocal
from app.models.device_token import DeviceToken
from app.models.employee import Employee


def _owner() -> str:
    """A unique owner key per test (device/employees tables aren't truncated)."""
    return f"EMP-{uuid.uuid4().hex[:12]}"


def _headers(owner: str, *, email: str | None = None, name: str | None = None) -> dict:
    headers = {"X-Employee-Id": owner}
    if email is not None:
        headers["X-Employee-Email"] = email
    if name is not None:
        headers["X-Employee-Name"] = name
    return headers


def _cleanup(owner: str) -> None:
    """Hard-delete the device_tokens / employees rows created for ``owner``."""
    session = SessionLocal()
    try:
        session.execute(delete(DeviceToken).where(DeviceToken.owner == owner))
        session.execute(delete(Employee).where(Employee.employee_id == owner))
        session.commit()
    finally:
        session.close()


def _tokens(db, owner: str) -> list[DeviceToken]:
    db.expire_all()
    return list(
        db.execute(select(DeviceToken).where(DeviceToken.owner == owner)).scalars()
    )


# ---------------------------------------------------------------------------
def test_register_upserts_single_row_by_owner(client, db) -> None:
    owner = _owner()
    try:
        resp = client.post(
            "/employee/device/register",
            json={"token": "tok-A", "platform": "ios"},
            headers=_headers(owner),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "registered"

        rows = _tokens(db, owner)
        assert len(rows) == 1
        first = rows[0]
        assert first.token == "tok-A"
        assert first.platform == "ios"
        assert first.status == Status.ACTIVE.value
        first_updated = first.updated_at

        # A SECOND register with a DIFFERENT token must NOT create a new row
        # (UNIQUE owner upsert): still exactly one, with the new token + a bumped
        # updated_at.
        resp = client.post(
            "/employee/device/register",
            json={"token": "tok-B", "platform": "android"},
            headers=_headers(owner),
        )
        assert resp.status_code == 200, resp.text

        rows = _tokens(db, owner)
        assert len(rows) == 1
        row = rows[0]
        assert row.token == "tok-B"
        assert row.platform == "android"
        assert row.status == Status.ACTIVE.value
        assert row.updated_at >= first_updated
        assert row.updated_at != first_updated
    finally:
        _cleanup(owner)


def test_register_self_populates_employee_roster(client, db) -> None:
    owner = _owner()
    email = "roster@corp.com"
    name = "Roster Person"
    try:
        resp = client.post(
            "/employee/device/register",
            json={"token": "tok"},
            headers=_headers(owner, email=email, name=name),
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        emp = db.execute(
            select(Employee).where(Employee.employee_id == owner)
        ).scalar_one_or_none()
        assert emp is not None
        assert emp.employee_id == owner
        assert emp.name == name
        assert emp.email == email

        # Re-register WITHOUT the X-Employee-Name header must NOT null-overwrite
        # the existing name (the null-overwrite guard). Email always resolves to
        # the Mode-A default when the header is absent, so only name is asserted.
        resp = client.post(
            "/employee/device/register", json={"token": "tok2"}, headers=_headers(owner)
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        emp = db.execute(
            select(Employee).where(Employee.employee_id == owner)
        ).scalar_one()
        assert emp.name == name  # preserved, not overwritten with null
    finally:
        _cleanup(owner)


def test_unregister_soft_deletes_and_is_idempotent(client, db) -> None:
    owner = _owner()
    try:
        client.post(
            "/employee/device/register", json={"token": "tok"}, headers=_headers(owner)
        )

        resp = client.post("/employee/device/unregister", json={}, headers=_headers(owner))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "unregistered"

        rows = _tokens(db, owner)
        assert len(rows) == 1
        assert rows[0].status == Status.DELETED.value

        # Idempotent: a second unregister is fine (still DELETED, still one row).
        resp = client.post("/employee/device/unregister", json={}, headers=_headers(owner))
        assert resp.status_code == 200, resp.text
        rows = _tokens(db, owner)
        assert len(rows) == 1
        assert rows[0].status == Status.DELETED.value
    finally:
        _cleanup(owner)


def test_unregister_without_prior_token_is_ok(client, db) -> None:
    owner = _owner()
    try:
        resp = client.post("/employee/device/unregister", json={}, headers=_headers(owner))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "unregistered"
        assert _tokens(db, owner) == []
    finally:
        _cleanup(owner)


def test_logout_clears_token(client, db) -> None:
    owner = _owner()
    try:
        client.post(
            "/employee/device/register", json={"token": "tok"}, headers=_headers(owner)
        )

        resp = client.post("/employee/logout", headers=_headers(owner))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "logged_out"

        rows = _tokens(db, owner)
        assert len(rows) == 1
        assert rows[0].status == Status.DELETED.value
    finally:
        _cleanup(owner)


def test_api_key_gate_enforced_when_configured(client, db, monkeypatch) -> None:
    """When EZ_CHAT_API_KEY is set, /employee/device/* requires a matching X-API-Key."""
    owner = _owner()
    monkeypatch.setenv("EZ_CHAT_API_KEY", "s3cret-key")
    get_settings.cache_clear()
    try:
        # No key -> 401.
        resp = client.post(
            "/employee/device/register", json={"token": "tok"}, headers=_headers(owner)
        )
        assert resp.status_code == 401, resp.text

        # Matching key -> allowed.
        headers = {**_headers(owner), "X-API-Key": "s3cret-key"}
        resp = client.post("/employee/device/register", json={"token": "tok"}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert _tokens(db, owner)[0].token == "tok"
    finally:
        _cleanup(owner)
        get_settings.cache_clear()
