"""Auth API routes.

OAuth2 password flow for the single hardcoded admin. ``POST /auth/login`` is
public and issues a bearer token; ``GET /auth/me`` is protected.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.security import authenticate_admin, create_access_token, get_current_admin
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
def login(form_data: OAuth2PasswordRequestForm = Depends()) -> dict[str, str]:
    """Issue a JWT access token for valid admin credentials (form-encoded)."""
    if not authenticate_admin(form_data.username, form_data.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "access_token": create_access_token(form_data.username),
        "token_type": "bearer",
    }


@router.get("/auth/me")
def me(current: dict = Depends(get_current_admin)) -> dict[str, str]:
    """Return the authenticated admin identity."""
    return {"username": current["username"]}
