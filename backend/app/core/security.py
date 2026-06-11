"""Security utilities for multi-tenant JWT auth.

PyJWT (HS256) token creation/decoding, bcrypt password hashing, and the FastAPI
auth dependencies (``get_current_user`` / ``require_admin`` / ``get_current_admin``).
The JWT ``sub`` claim carries the user **id** (as a string) and a ``role`` claim;
identity is resolved against the ``users`` table at request time.

Settings are read at call time via ``get_settings()`` so test/env overrides
(conftest cache-clear/rebind) are honored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.base import Status
from app.database.session import get_db
from app.models.user import User

# Relative tokenUrl (no leading slash) so it resolves correctly under any mount;
# matches the actual login path POST /auth/login.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

_FORBIDDEN_EXCEPTION = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Admin privileges required",
)


# --- Password hashing (bcrypt directly; passlib/bcrypt5 is broken) -----------
def hash_password(password: str) -> str:
    """Return a bcrypt hash of ``password`` (UTF-8, salted) as a string."""
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of ``password`` against a bcrypt ``password_hash``.

    Returns ``False`` (never raises) on a malformed/empty stored hash so callers
    can treat any failure uniformly as "bad credentials".
    """
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --- JWT ---------------------------------------------------------------------
def create_access_token(
    subject: str,
    role: str = "user",
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed JWT with ``sub`` (user id), ``role`` and ``exp`` claims."""
    settings = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    expire = datetime.now(UTC) + expires_delta
    payload = {"sub": str(subject), "role": role, "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises ``jwt.PyJWTError`` on invalid/expired tokens."""
    settings = get_settings()
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


# --- Auth dependencies -------------------------------------------------------
def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the bearer token to an active :class:`User`.

    Raises 401 on any invalid/expired token, an unparsable subject, a missing
    user, a soft-deleted user (``status != ACTIVE``), or a disabled user
    (``is_active == False``).
    """
    return _resolve_user_from_token(token, db)


# OAuth2 scheme that does NOT auto-error when the Authorization header is absent,
# so the file-serve route can fall back to a ``?token=`` query param for plain
# browser navigations (which cannot send an Authorization header).
_oauth2_optional = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


def _resolve_user_from_token(token: str, db: Session) -> User:
    """Decode a JWT, load the active user, or raise the 401 credentials error.

    Shared by the header- and query-param auth paths so both verify the token the
    exact same way (same secret/alg) and apply the identical active-user checks.
    """
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as exc:
        raise _CREDENTIALS_EXCEPTION from exc

    subject = payload.get("sub")
    if subject is None:
        raise _CREDENTIALS_EXCEPTION
    try:
        user_id = int(subject)
    except (TypeError, ValueError) as exc:
        raise _CREDENTIALS_EXCEPTION from exc

    user = db.get(User, user_id)
    if user is None or user.status != Status.ACTIVE.value or not user.is_active:
        raise _CREDENTIALS_EXCEPTION
    return user


def get_current_user_query_or_header(
    token: str | None = Depends(_oauth2_optional),
    query_token: str | None = Query(default=None, alias="token"),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the current user from the Bearer header OR a ``?token=`` query param.

    Plain top-level browser navigations (e.g. opening a document/source link in a
    new tab) cannot send an ``Authorization: Bearer`` header. This dependency
    keeps the normal header path for API callers and, only when the header is
    absent, falls back to a short-lived access token passed as ``?token=<jwt>``.
    The token is verified with the same secret/algorithm and the same active-user
    checks as :func:`get_current_user`; ownership is still enforced by the route.
    """
    effective = token or query_token
    if not effective:
        raise _CREDENTIALS_EXCEPTION
    return _resolve_user_from_token(effective, db)


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Require the authenticated user to have the ``admin`` role (else 403)."""
    if current_user.role != "admin":
        raise _FORBIDDEN_EXCEPTION
    return current_user


def get_current_admin(current_user: User = Depends(require_admin)) -> User:
    """Backwards-compatible alias for :func:`require_admin`.

    Existing admin/document routes depend on ``get_current_admin``; it is now
    role-based (the admin is simply a seeded ``User`` with ``role == 'admin'``).
    """
    return current_user
