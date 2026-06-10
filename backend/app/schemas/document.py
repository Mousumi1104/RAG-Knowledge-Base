"""Pydantic v2 schemas for the document API contract.

These DTOs are decoupled from the ORM model; ``DocumentOut`` reads from ORM
instances via ``from_attributes``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DocumentUploadRequest(BaseModel):
    """Optional metadata accepted alongside a multipart file upload."""

    uploaded_by: str | None = Field(
        default="admin", description="Identity of the uploader (e.g. email/admin)."
    )


class DocumentOut(BaseModel):
    """Serialized representation of a :class:`~app.models.document.Document`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: int
    added_time: datetime
    last_modified: datetime
    filename: str
    document_type: str
    uploaded_by: str | None = None
    # Tenant ownership. Listing/get routes are owner-scoped server-side, so these
    # never leak another user's rows; they let the client distinguish own vs.
    # (admin-managed) global docs. ``owner_id`` is null for global/legacy docs.
    owner_id: int | None = None
    is_global: bool = False
    file_path: str
    size: int
    indexing_status: str
    chunk_count: int
    error: str | None = None


class DocumentList(BaseModel):
    """A paginated-friendly list wrapper for documents."""

    items: list[DocumentOut]
    total: int


class MessageResponse(BaseModel):
    """Generic structured message response (e.g. for deletes)."""

    detail: str


class ReindexResponse(BaseModel):
    """Response shape for a (re)index trigger."""

    id: int
    indexing_status: str
    detail: str
