"""Tests for the Employee Zone identity seam (auth-disabled mode).

Covers the pure retrieval-scope helper, JIT employee provisioning/reconciliation,
and the header-driven identity resolution used while ``EZ_AUTH_ENABLED`` is False.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.core import employee_zone
from app.core.access import GLOBAL_ONLY, retrieval_scope
from app.core.config import get_settings
from app.database.base import Status
from app.models.user import User
from app.services import user_service


def _email() -> str:
    """A unique email per test (users table is not truncated between tests)."""
    return f"ez-{uuid.uuid4().hex[:12]}@example.com"


class _Req:
    """Minimal stand-in for a Starlette request exposing ``.headers``."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


# --- retrieval_scope ---------------------------------------------------------
def test_retrieval_scope_employee_is_global_only(db) -> None:
    user = user_service.provision_employee(db, _email())
    assert user.role == "user"
    assert retrieval_scope(user) == GLOBAL_ONLY == (None, True)


def test_retrieval_scope_admin_keeps_own_plus_global(db) -> None:
    admin = user_service.create_user(db, _email(), "pw", role="admin")
    assert retrieval_scope(admin) == (admin.id, True)


# --- provision_employee ------------------------------------------------------
def test_provision_employee_jit_creates_active_user_role(db) -> None:
    email = _email()
    user = user_service.provision_employee(db, email, employee_id="E1", full_name="Em P")

    assert user.id is not None
    assert user.email == email
    assert user.role == "user"
    assert user.is_active is True
    assert user.status == Status.ACTIVE.value
    # Password hash is non-null but unusable: email/password login must never match.
    assert user.password_hash
    assert user_service.authenticate_user(db, email, "pw") is None


def test_provision_employee_returns_existing_without_role_change(db) -> None:
    email = _email()
    admin = user_service.create_user(db, email, "pw", role="admin")

    returned = user_service.provision_employee(db, email)

    assert returned.id == admin.id
    assert returned.role == "admin"  # never downgraded/changed


def test_provision_employee_disabled_user_raises_403(db) -> None:
    email = _email()
    user = user_service.create_user(db, email, "pw")
    user.is_active = False
    db.add(user)
    db.commit()

    with pytest.raises(user_service.UserDisabledError) as exc:
        user_service.provision_employee(db, email)
    assert exc.value.status_code == 403


def test_provision_employee_normalizes_email(db) -> None:
    base = _email()
    first = user_service.provision_employee(db, base.upper())
    second = user_service.provision_employee(db, f"  {base}  ")
    assert first.id == second.id
    assert first.email == base


# --- resolve_employee (auth disabled) ----------------------------------------
def test_resolve_employee_uses_default_email_when_no_header(db) -> None:
    settings = get_settings()
    assert settings.EZ_AUTH_ENABLED is False  # current mode

    user = employee_zone.resolve_employee(db, _Req())
    assert user.email == settings.EZ_DEFAULT_EMPLOYEE_EMAIL
    assert user.role == "user"


def test_resolve_employee_honors_email_header(db) -> None:
    email = _email()
    req = _Req(
        {"X-Employee-Email": email, "X-Employee-Id": "E7", "X-Employee-Name": "Nm"}
    )
    user = employee_zone.resolve_employee(db, req)
    assert user.email == email
    assert user.role == "user"


def test_resolve_employee_enabled_mode_is_stubbed(db, monkeypatch) -> None:
    # Flipping the switch on without the (future) verifier must fail loudly, not
    # silently bypass verification.
    from app.core import config

    real = config.Settings()
    monkeypatch.setattr(real, "EZ_AUTH_ENABLED", True)
    monkeypatch.setattr(employee_zone, "get_settings", lambda: real)

    with pytest.raises(HTTPException) as exc:
        employee_zone.resolve_employee(db, _Req())
    assert exc.value.status_code == 501
