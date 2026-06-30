"""Employee Zone identity-resolution seam used by the /employee/* endpoints.

This module is the single place that turns an incoming Employee Zone request into
a resolved :class:`~app.models.user.User`. It has two modes, selected by
``settings.EZ_AUTH_ENABLED``:

**Mode A — auth DISABLED (``EZ_AUTH_ENABLED is False``, the CURRENT mode):**
    No token is required. Optional, *unverified* identity headers are read if the
    client chooses to send them:

    - ``X-Employee-Email`` — the employee's email (used as the provisioning key);
    - ``X-Employee-Id``    — an opaque employee id (forwarded for the future seam);
    - ``X-Employee-Name``  — the employee's display name (optional).

    When no email header is present, the shared
    ``settings.EZ_DEFAULT_EMPLOYEE_EMAIL`` is used. The resolved email is passed
    to :func:`user_service.provision_employee`, which JIT-provisions (or
    reconciles) the backing user. This lets the app optionally separate
    conversations per employee *now*, with zero authentication.

**Mode B — auth ENABLED (``EZ_AUTH_ENABLED is True``, FUTURE):**
    A real Employee Zone access token will be required and verified according to
    ``settings.EZ_VERIFY_MODE`` (introspection / JWKS / HS256 shared-secret),
    then the verified ``email``/``emp_id``/``name`` claims will be threaded into
    :func:`user_service.provision_employee`. This is intentionally left as a
    clearly-marked TODO stub: real verification is NOT implemented in this task.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import User
from app.services import user_service

# Optional unverified identity headers (Mode A only).
_HEADER_EMPLOYEE_ID = "X-Employee-Id"
_HEADER_EMPLOYEE_EMAIL = "X-Employee-Email"
_HEADER_EMPLOYEE_NAME = "X-Employee-Name"
_HEADER_API_KEY = "X-API-Key"


def require_api_key(
    x_api_key: str | None = Header(default=None, alias=_HEADER_API_KEY),
) -> None:
    """Gate /employee/* with the shared EZ chat API key (constant-time compare).

    When ``EZ_CHAT_API_KEY`` is unset the gate is a no-op (rollout window); once
    set in Render every /employee/* request must present a matching ``X-API-Key``.
    The key is embedded in the mobile client and therefore extractable — this is a
    low-assurance gate against casual/anonymous abuse, not authenticated identity
    (see EZ chat auth spec §2/§4).
    """
    expected = get_settings().EZ_CHAT_API_KEY
    if not expected:
        return  # not configured yet -> fail open (rollout window)
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


def resolve_employee(db: Session, request: Request) -> User:
    """Resolve the current Employee Zone employee to a backing :class:`User`.

    Dispatches on ``settings.EZ_AUTH_ENABLED`` (see the module docstring). In the
    current (disabled) mode this never requires a token; in the future (enabled)
    mode it will verify a real Employee Zone access token. Returns the
    provisioned/reconciled active user.
    """
    settings = get_settings()

    if settings.EZ_AUTH_ENABLED:
        return _resolve_employee_verified(db, request, settings)

    # --- Mode A: auth disabled -- read optional unverified headers -----------
    headers = request.headers
    employee_id = headers.get(_HEADER_EMPLOYEE_ID)
    employee_name = headers.get(_HEADER_EMPLOYEE_NAME)
    email = headers.get(_HEADER_EMPLOYEE_EMAIL) or settings.EZ_DEFAULT_EMPLOYEE_EMAIL

    return user_service.provision_employee(
        db,
        email=email,
        employee_id=employee_id,
        full_name=employee_name,
    )


def _resolve_employee_verified(db: Session, request: Request, settings) -> User:
    """Verify a real Employee Zone access token and resolve the employee (FUTURE).

    TODO(employee-zone-auth): implement real verification once Employee Zone
    issues tokens. Planned flow, selected by ``settings.EZ_VERIFY_MODE``:

      1. Extract the bearer access token from the ``Authorization`` header
         (generic 401 if missing/malformed — never leak which check failed).
      2. Verify it according to ``EZ_VERIFY_MODE``:
           - "introspection": POST to ``EZ_INTROSPECTION_URL``, require ``active``;
           - "jwks": fetch ``EZ_JWKS_URL`` (cache + refetch on kid miss), pin the
             asymmetric alg, verify signature;
           - "hs256": verify HS256 with ``EZ_SHARED_SECRET``.
         For all modes require ``iss == EZ_ISSUER`` and ``EZ_AUDIENCE`` in ``aud``,
         plus exp/nbf/iat with clock skew.
      3. Extract identity from claims ``EZ_EMAIL_CLAIM`` / ``EZ_EMPLOYEE_ID_CLAIM``
         / ``EZ_NAME_CLAIM``.
      4. Call ``user_service.provision_employee(...)`` (role is forced to "user";
         a disabled/off-boarded user raises 403) and return the User.

    Until then this is a not-implemented stub so flipping ``EZ_AUTH_ENABLED`` on
    fails loudly rather than silently bypassing verification.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Employee Zone token verification is not implemented yet",
    )
