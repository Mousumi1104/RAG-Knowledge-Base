"""Pydantic v2 schemas for the auth / user API contract.

``UserOut`` is the only user representation ever returned to clients; it
deliberately omits ``password_hash``. ``UserCreate`` validates registration
input (valid email + a minimum-length password).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    """Registration payload: a valid email and a password (min length 8)."""

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)


class UserOut(BaseModel):
    """Safe public representation of a user (never exposes ``password_hash``).

    ``email`` is a plain ``str`` on output (not ``EmailStr``): it is validated on
    input at registration, and the seeded admin's login email (``admin@local``)
    is intentionally not a public-DNS address, so re-validating on output would
    wrongly reject it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: str
    is_active: bool


class LoginResponse(BaseModel):
    """OAuth2-style bearer token response."""

    access_token: str
    token_type: str = "bearer"
