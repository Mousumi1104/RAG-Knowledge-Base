"""Employee-Zone device-token + logout API routes.

Registers/clears the single push-notification device token per Employee-Zone
**owner key** so the (future) reminder job can push to logged-in employees. The
owner key is the SAME value the chat-history stack uses:
:func:`app.services.employee_chat_service.resolve_owner` — the ``X-Employee-Id``
header, falling back to the resolved employee email (Mode-A identity).

Endpoints (all gated by :func:`require_api_key`, same as ``/employee/*``):
- ``POST /employee/device/register``   — upsert the caller's ``device_tokens`` row
  (UNIQUE by owner) AND self-populate the ``employees`` roster row from the login.
- ``POST /employee/device/unregister`` — idempotent soft-delete of the caller's
  token row.
- ``POST /employee/logout``            — same server-side token clear as unregister.

There is intentionally no user/Bearer auth here: identity is the Employee-Zone
owner key, not the users-table :func:`get_current_user`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.employee import _owner_identity, get_current_employee
from app.core.employee_zone import require_api_key
from app.database.base import Status
from app.database.session import get_db
from app.models.device_token import DeviceToken
from app.models.employee import Employee
from app.models.user import User
from app.schemas.device import (
    DeviceActionResponse,
    DeviceRegisterRequest,
    DeviceUnregisterRequest,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Prefix "/employee": paths are POST /employee/device/register,
# /employee/device/unregister and /employee/logout.
# The shared EZ API-key gate applies to every route (same as /employee/*).
router = APIRouter(
    prefix="/employee",
    tags=["device"],
    dependencies=[Depends(require_api_key)],
)


def _upsert_employee_row(
    db: Session, *, owner: str, email: str | None, name: str | None
) -> None:
    """Self-populate the ``employees`` roster from a login (never null-overwrite).

    Inserts a roster row keyed by ``employee_id=owner`` when missing, otherwise
    reconciles it — but a non-null existing ``name``/``email`` is NEVER clobbered
    with a null. ``name`` is NOT NULL, so a missing name falls back to the email
    then the owner key on first insert.
    """
    row = db.execute(
        select(Employee).where(Employee.employee_id == owner)
    ).scalar_one_or_none()
    if row is None:
        db.add(
            Employee(
                employee_id=owner,
                name=(name or email or owner),
                email=email,
            )
        )
        return
    if name:
        row.name = name
    if email:
        row.email = email
    db.add(row)


def _clear_token(db: Session, owner: str) -> bool:
    """Soft-delete the caller's device token (idempotent). Returns True if cleared."""
    row = db.execute(
        select(DeviceToken).where(DeviceToken.owner == owner)
    ).scalar_one_or_none()
    if row is None or row.status == Status.DELETED.value:
        return False
    row.status = Status.DELETED.value
    row.updated_at = func.now()
    db.add(row)
    db.commit()
    return True


@router.post("/device/register", response_model=DeviceActionResponse)
def device_register(
    payload: DeviceRegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> DeviceActionResponse:
    """Upsert the caller's push device token (UNIQUE by owner) + roster row.

    One ACTIVE token per owner: an existing row is updated in place
    (token/platform/updated_at, re-activated) rather than duplicated.
    """
    owner, email, name = _owner_identity(request, employee)
    logger.info("POST /device/register owner=%r platform=%r", owner, payload.platform)

    row = db.execute(
        select(DeviceToken).where(DeviceToken.owner == owner)
    ).scalar_one_or_none()
    if row is None:
        db.add(
            DeviceToken(
                owner=owner,
                token=payload.token,
                platform=payload.platform,
                status=Status.ACTIVE.value,
            )
        )
    else:
        row.token = payload.token
        row.platform = payload.platform
        row.status = Status.ACTIVE.value
        row.updated_at = func.now()
        db.add(row)

    _upsert_employee_row(db, owner=owner, email=email, name=name)
    db.commit()
    return DeviceActionResponse(status="registered")


@router.post("/device/unregister", response_model=DeviceActionResponse)
def device_unregister(
    request: Request,
    payload: DeviceUnregisterRequest | None = None,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> DeviceActionResponse:
    """Idempotent soft-delete of the caller's device token (ok if none exists)."""
    owner, _email, _name = _owner_identity(request, employee)
    logger.info("POST /device/unregister owner=%r", owner)
    _clear_token(db, owner)
    return DeviceActionResponse(status="unregistered")


@router.post("/logout", response_model=DeviceActionResponse)
def logout(
    request: Request,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> DeviceActionResponse:
    """Server-side token clear for the caller (same effect as unregister)."""
    owner, _email, _name = _owner_identity(request, employee)
    logger.info("POST /logout owner=%r", owner)
    _clear_token(db, owner)
    return DeviceActionResponse(status="logged_out")
