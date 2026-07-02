"""Pydantic v2 schemas for the Employee-Zone device-token API contract.

The device endpoints register/clear the single push token per Employee-Zone
owner (see :class:`~app.models.device_token.DeviceToken`). The reminder job later
pushes to the ACTIVE token; ``/logout`` and ``/device/unregister`` soft-delete it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DeviceRegisterRequest(BaseModel):
    """Register (upsert) the caller's push device token."""

    token: str = Field(..., description="Opaque push-notification device token.")
    platform: str | None = Field(
        default=None,
        description="Client platform hint (e.g. 'ios' / 'android'), if supplied.",
    )


class DeviceUnregisterRequest(BaseModel):
    """Unregister the caller's push device token (soft-delete).

    ``token`` is optional/ignored: the row is keyed by the resolved owner, so the
    caller's identity alone selects the row to clear.
    """

    token: str | None = Field(
        default=None,
        description="Ignored; the row is keyed by the resolved owner identity.",
    )


class DeviceActionResponse(BaseModel):
    """Result of a device-token action (register / unregister / logout)."""

    status: str = Field(..., description="Action outcome, e.g. 'registered'.")
    message: str | None = Field(
        default=None, description="Optional human-readable detail."
    )
