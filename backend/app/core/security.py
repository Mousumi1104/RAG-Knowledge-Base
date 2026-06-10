"""Security utilities for single-admin JWT auth.

PyJWT (HS256) token creation/decoding, constant-time credential checks, and the
``get_current_admin`` FastAPI dependency. Settings are read at call time via
``get_settings()`` so test/env overrides (conftest cache-clear/rebind) are honored.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import get_settings

# Relative tokenUrl (no leading slash) so it resolves correctly under any mount;
# matches the actual login path POST /auth/login.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def create_access_token(subject: str, expires_delta: timedelta | None = None) -> str:
    """Create a signed JWT for ``subject`` with ``sub`` and ``exp`` claims."""
    settings = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    expire = datetime.now(UTC) + expires_delta
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises ``jwt.PyJWTError`` on invalid/expired tokens."""
    settings = get_settings()
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


def authenticate_admin(username: str, password: str) -> bool:
    """Constant-time check of credentials against the configured admin."""
    settings = get_settings()
    username_ok = secrets.compare_digest(username, settings.ADMIN_USERNAME)
    password_ok = secrets.compare_digest(password, settings.ADMIN_PASSWORD)
    return username_ok and password_ok


def get_current_admin(token: str = Depends(oauth2_scheme)) -> dict:
    """Validate the bearer token and return the admin identity.

    Raises 401 (with ``WWW-Authenticate: Bearer``) on any invalid/expired token
    or when the subject does not match the configured admin username.
    """
    settings = get_settings()
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise _CREDENTIALS_EXCEPTION from exc
    except jwt.InvalidTokenError as exc:
        raise _CREDENTIALS_EXCEPTION from exc

    subject = payload.get("sub")
    if subject is None or subject != settings.ADMIN_USERNAME:
        raise _CREDENTIALS_EXCEPTION
    return {"username": settings.ADMIN_USERNAME}
