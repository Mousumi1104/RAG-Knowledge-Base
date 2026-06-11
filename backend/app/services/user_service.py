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


def list_users(db: Session, exclude_admins: bool = False) -> list[User]:
    """Return non-deleted users, newest first.

    When ``exclude_admins`` is set, super-admin accounts (``role == 'admin'``)
    are filtered out so the admin user-management list never surfaces the
    super-admin (including the requesting admin) as a manageable/deletable user.
    """
    stmt = select(User).where(User.status != Status.DELETED.value)
    if exclude_admins:
        stmt = stmt.where(User.role != "admin")
    stmt = stmt.order_by(User.added_time.desc(), User.id.desc())
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


def resolve_admin_email() -> str:
    """Resolve the seeded admin login email deterministically.

    Prefer ``ADMIN_EMAIL`` if set; otherwise derive from ``ADMIN_USERNAME``
    (verbatim if it already looks like an email, else ``<username>@local``).
    This mirrors the 0004 migration's ``_admin_email`` so the startup seed and
    the migration agree on exactly one address.
    """
    from app.core.config import get_settings

    settings = get_settings()
    email = (settings.ADMIN_EMAIL or "").strip()
    if email:
        return normalize_email(email)
    username = (settings.ADMIN_USERNAME or "admin").strip()
    return normalize_email(username if "@" in username else f"{username}@local")


def seed_admin_user(db: Session) -> User:
    """Idempotently upsert the configured admin user (self-healing on every boot).

    Resolves a deterministic admin email (see :func:`resolve_admin_email`) and:

    - creates the admin if missing, or
    - if present, re-hashes the configured ``ADMIN_PASSWORD`` into
      ``password_hash``, forces ``role='admin'``, ``is_active=True`` and
      ``status=ACTIVE`` (un-deletes a soft-deleted admin).

    This makes the live DB repair itself on redeploy so ``/auth/login`` with the
    configured admin credentials always succeeds with a ``role=admin`` JWT. The
    lookup is done directly (not via :func:`get_user_by_email`, which filters out
    DELETED rows) so a soft-deleted admin row is reactivated rather than
    duplicated against the UNIQUE email index.
    """
    from app.core.config import get_settings

    settings = get_settings()
    email = resolve_admin_email()
    password = settings.ADMIN_PASSWORD

    stmt = select(User).where(User.email == email)
    user = db.execute(stmt).scalars().first()

    if user is None:
        user = User(
            email=email,
            password_hash=hash_password(password),
            role="admin",
            is_active=True,
            status=Status.ACTIVE.value,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # Lost a race with a concurrent seeder / the migration seed; reload.
            db.rollback()
            user = db.execute(stmt).scalars().first()
            if user is None:
                raise
        else:
            db.refresh(user)
            logger.info("Seeded admin user id=%s email=%s", user.id, email)
            return user

    # Self-heal an existing row: re-hash password, force admin role + active.
    user.password_hash = hash_password(password)
    user.role = "admin"
    user.is_active = True
    user.status = Status.ACTIVE.value
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Reconciled admin user id=%s email=%s", user.id, email)
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


def _cascade_delete_user_documents(db: Session, user_id: int) -> int:
    """Delete every document OWNED by ``user_id`` (DB row + vector chunks).

    Each document goes through the canonical
    :func:`app.services.document_service.soft_delete_document` path so vector
    chunks/embeddings (and any remote-stored file) are cleaned up consistently —
    no orphaned vectors are left behind. Admin-global / un-owned documents
    (``owner_id IS NULL`` / ``is_global``) are intentionally NOT matched, so
    global docs that ground every user's answers are preserved.

    Idempotent: only ACTIVE, owner-matched rows are selected, so re-running on an
    already-cleaned user is a no-op. Returns the number of documents removed.
    """
    # Imported lazily to avoid a heavy import (indexing/vector store) at module
    # load and to sidestep any import cycle between the services.
    from app.services import document_service

    owned = document_service.list_documents(db, owner_id=user_id)
    deleted = 0
    for doc in owned:
        # owner-scoped delete: confirms ownership and removes the doc + its
        # vectors/chunks via the single canonical deletion path.
        document_service.soft_delete_document(db, doc.id, owner_id=user_id)
        deleted += 1
    if deleted:
        logger.info("Cascade-deleted %s document(s) for user id=%s", deleted, user_id)
    return deleted


def _cascade_delete_user_conversations(db: Session, user_id: int) -> int:
    """Soft-delete every conversation owned by ``user_id`` (status = DELETED).

    Idempotent: only currently non-deleted conversations are matched. Returns the
    number of conversations soft-deleted.
    """
    from app.models.conversation import Conversation

    stmt = select(Conversation).where(
        Conversation.user_id == user_id,
        Conversation.status != Status.DELETED.value,
    )
    convos = list(db.execute(stmt).scalars().all())
    for convo in convos:
        convo.status = Status.DELETED.value
        db.add(convo)
    if convos:
        db.commit()
        logger.info(
            "Cascade-deleted %s conversation(s) for user id=%s", len(convos), user_id
        )
    return len(convos)


def delete_user(db: Session, user_id: int) -> User:
    """Soft-delete a user AND cascade-delete the data they own.

    Order of operations:

    1. Resolve the (non-deleted) user (404 otherwise).
    2. Delete all documents the user OWNS (``owner_id == user_id``) via the
       canonical :func:`document_service.soft_delete_document` path so their
       vector chunks/embeddings are removed — leaving no orphaned vectors.
       Admin-global / un-owned docs are preserved.
    3. Soft-delete the user's conversations.
    4. Soft-delete the user row (status = DELETED).

    The self/admin guard lives in the API layer and remains in force; this
    function is only reached for a deletable, non-self user.
    """
    user = get_user_or_404(db, user_id)

    # Clean up owned data first so a failure here aborts before the user row is
    # marked deleted (the operation can then be safely retried; it is idempotent).
    _cascade_delete_user_documents(db, user_id)
    _cascade_delete_user_conversations(db, user_id)

    user.status = Status.DELETED.value
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Soft-deleted user id=%s (with owned documents + conversations)", user_id)
    return user
