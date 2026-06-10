"""Pydantic v2 schemas for the persisted conversation/message API contract.

These DTOs are decoupled from the ORM models; they read from ORM instances via
``from_attributes``. ``MessageOut.sources`` reuses the chat ``Source`` shape.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.chat import Source


class MessageOut(BaseModel):
    """Serialized representation of a :class:`~app.models.message.Message`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    sources: list[Source] | None = None
    added_time: datetime


class ConversationOut(BaseModel):
    """Serialized representation of a :class:`~app.models.conversation.Conversation`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str | None = None
    added_time: datetime
    last_modified: datetime


class ConversationList(BaseModel):
    """A list wrapper for conversations."""

    items: list[ConversationOut]
    total: int


class ConversationDetail(ConversationOut):
    """A conversation plus its chronological messages."""

    messages: list[MessageOut] = []
