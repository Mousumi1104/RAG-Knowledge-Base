"""User account business logic.

Registration, authentication, and admin user-management operations live here so
the API layer stays thin. Email is normalized (trimmed + lowercased) on write;
the ``users.email`` column is CITEXT so lookups are case-insensitive regardless.
Passwords are hashed with bcrypt (see :mod:`app.core.security`).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.database.base import Status
from app.models.user import User
from app.utils.errors import AppError, NotFoundError
from app.utils.logging import get_logger

logger = get_logger(__name__)


class DuplicateEmailError(AppError):
    """A user with the given email already exists."""

    status_code = 409
    detail = "Email already registered"


def normalize_email(email: str) -> str:
    """Trim and lowercase an email for consistent storage/lookup."""
    return email.strip().lower()


def get_user_by_email(db: Session, email: str) -> User | None:
    """Return the (non-deleted) user with this email, or ``None``."""
    stmt = select(User).where(
        User.email == normalize_email(email),
        User.status != Status.DELETED.value,
    )
    return db.execute(stmt).scalars().first()


def get_user(db: Session, user_id: int) -> User | None:
    """Return one non-deleted user by id, or ``None``."""
    user = db.get(User, user_id)
    if user is None or user.status == Status.DELETED.value:
        return None
    return user


def get_user_or_404(db: Session, user_id: int) -> User:
    """Return a non-deleted user by id or raise :class:`NotFoundError`."""
    user = get_user(db, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    return user


def list_users(db: Session) -> list[User]:
    """Return all non-deleted users, newest first."""
    stmt = (
        select(User)
        .where(User.status != Status.DELETED.value)
        .order_by(User.added_time.desc(), User.id.desc())
    )
    return list(db.execute(stmt).scalars().all())


def create_user(db: Session, email: str, password: str, role: str = "user") -> User:
    """Create an active user with a bcrypt-hashed password.

    The email is normalized and duplicates are rejected with
    :class:`DuplicateEmailError` (a 409). The unique index is the authoritative
    guard; the pre-check is a friendly fast path.
    """
    normalized = normalize_email(email)
    if get_user_by_email(db, normalized) is not None:
        raise DuplicateEmailError()

    user = User(
        email=normalized,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        status=Status.ACTIVE.value,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise DuplicateEmailError() from exc
    db.refresh(user)
    logger.info("Created user id=%s email=%s role=%s", user.id, normalized, role)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """Return the user iff the password matches and the account is usable.

    "Usable" means non-deleted (``status == ACTIVE``) and ``is_active``. Returns
    ``None`` on any failure so callers can emit a single generic 401.
    """
    user = get_user_by_email(db, email)
    if user is None:
        return None
    if user.status != Status.ACTIVE.value or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def set_active(db: Session, user_id: int, active: bool) -> User:
    """Enable/disable a user (the admin ``is_active`` toggle)."""
    user = get_user_or_404(db, user_id)
    user.is_active = active
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Set user id=%s is_active=%s", user_id, active)
    return user


def delete_user(db: Session, user_id: int) -> User:
    """Soft-delete a user (status = DELETED)."""
    user = get_user_or_404(db, user_id)
    user.status = Status.DELETED.value
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Soft-deleted user id=%s", user_id)
    return user
