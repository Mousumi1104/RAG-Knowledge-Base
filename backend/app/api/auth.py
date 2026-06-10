"""Auth API routes.

Email + password auth backed by the ``users`` table. ``POST /auth/register``
(gated by ``settings.ALLOW_REGISTRATION``) and ``POST /auth/login`` are public;
``GET /auth/me`` is protected. Login keeps the OAuth2 password form, treating the
``username`` field as the user's email.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import create_access_token, get_current_user
from app.database.session import get_db
from app.models.user import User
from app.schemas.user import LoginResponse, UserCreate, UserOut
from app.services import user_service
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["auth"])


@router.post("/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    """Self-register a new ``user`` account (gated by ALLOW_REGISTRATION).

    Duplicate emails return 409 (raised as ``DuplicateEmailError``). The response
    is a safe ``UserOut`` and never includes the password hash.
    """
    if not get_settings().ALLOW_REGISTRATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled",
        )
    user = user_service.create_user(db, payload.email, payload.password, role="user")
    logger.info("Registered user id=%s", user.id)
    return user


@router.post("/auth/login", response_model=LoginResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Issue a JWT for valid credentials (``username`` field carries the email)."""
    user = user_service.authenticate_user(db, form_data.username, form_data.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(subject=str(user.id), role=user.role)
    return LoginResponse(access_token=token, token_type="bearer")


@router.get("/auth/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> User:
    """Return the authenticated user's safe identity (id, email, role)."""
    return current_user
