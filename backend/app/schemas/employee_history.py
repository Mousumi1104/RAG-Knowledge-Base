"""Pydantic v2 schemas for the Employee-Zone chat-history API contract.

Mirrors the server-side history spec: conversation list items carry
``created_at``/``updated_at`` (not the ORM's ``added_time``/``last_modified``),
``message_count`` and a ``preview`` snippet; the list response is cursor
paginated (``next_cursor`` + ``has_more``). These DTOs are built explicitly from
ORM rows in ``employee_chat_service`` (no ``from_attributes``) so the JSON keys
match the app's expectations.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.chat import Source


class EmployeeMessageOut(BaseModel):
    """A single persisted Employee-Zone chat message."""

    id: int
    conversation_id: int
    role: str
    content: str
    sources: list[Source] | None = None
    created_at: datetime


class EmployeeConversationSummary(BaseModel):
    """Lightweight conversation summary for the history list (no message bodies)."""

    id: int
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int
    preview: str | None = Field(
        default=None, description="Short snippet of the latest message."
    )


class EmployeeConversationListResponse(BaseModel):
    """Cursor-paginated list of the caller's conversations, newest first."""

    conversations: list[EmployeeConversationSummary]
    next_cursor: str | None = None
    has_more: bool = False


class EmployeeConversationDetail(BaseModel):
    """A conversation plus its chronological (oldest->newest) messages."""

    id: int
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int
    messages: list[EmployeeMessageOut] = []


class ConversationCreateRequest(BaseModel):
    """Optional body for explicit conversation creation."""

    title: str | None = Field(default=None, max_length=512)


class ConversationRenameRequest(BaseModel):
    """Body for renaming a conversation title."""

    title: str = Field(..., description="New conversation title (non-empty once trimmed).")


class ConversationDeleteResponse(BaseModel):
    """Idempotent delete acknowledgement."""

    deleted: bool = True
    id: int
