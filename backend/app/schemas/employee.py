"""Pydantic v2 schemas for the Employee Zone API contract.

The Employee Zone chat routes reuse the existing chat/conversation schemas
(``ChatRequest``/``ChatResponse``/``Source``/``Conversation*``); only the
identity-echo response for ``GET /employee/auth/me`` is unique to this surface.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EmployeeMeResponse(BaseModel):
    """The resolved Employee Zone identity backing the current request.

    ``id``/``email``/``role`` come from the provisioned backing :class:`User`.
    ``emp_id``/``full_name`` are the optional, unverified identity hints the
    client sent via the ``X-Employee-Id``/``X-Employee-Name`` headers (not yet
    persisted on the user row), echoed back for the caller's convenience.
    """

    id: int = Field(..., description="Id of the backing user row for this employee.")
    email: str = Field(..., description="Resolved employee email (provisioning key).")
    role: str = Field(..., description="Backing user role (always 'user' for employees).")
    emp_id: str | None = Field(
        default=None,
        description="Opaque employee id from the X-Employee-Id header, if supplied.",
    )
    full_name: str | None = Field(
        default=None,
        description="Display name from the X-Employee-Name header, if supplied.",
    )
