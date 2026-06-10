"""Pydantic v2 schemas for the RAG chat API contract.

The response mirrors the REQUIREMENTS/README example shape
``{answer, sources: [{document, excerpt}]}`` and augments each source with
traceable identifiers (``document_id``, ``score``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """A single prior conversation turn for follow-up support."""

    role: str = Field(..., description="Message author role, e.g. 'user' or 'assistant'.")
    content: str = Field(..., description="The message text.")


class ChatRequest(BaseModel):
    """A natural-language chat/RAG request."""

    query: str = Field(
        ...,
        min_length=1,
        description="The user's natural-language question.",
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        description="Max number of context chunks to retrieve (defaults to settings.TOP_K).",
    )
    history: list[ChatMessage] | None = Field(
        default=None,
        description="Optional prior conversation turns for follow-up questions.",
    )
    conversation_id: int | None = Field(
        default=None,
        description=(
            "Optional id of an existing conversation to continue. When omitted "
            "or null, a new conversation is created and its id is returned."
        ),
    )


class Source(BaseModel):
    """A traceable source reference contributing to the answer."""

    document: str = Field(..., description="Source document filename.")
    excerpt: str = Field(..., description="The grounding excerpt from the document.")
    document_id: int = Field(..., description="Id of the source document.")
    page: int | None = Field(
        default=None,
        description="1-based source page the excerpt came from (null if unknown).",
    )
    score: float = Field(..., description="Similarity score of the contributing chunk.")


class ChatResponse(BaseModel):
    """A grounded answer plus the sources it was built from."""

    answer: str = Field(..., description="The grounded, context-based answer.")
    sources: list[Source] = Field(
        default_factory=list,
        description="Source references used to ground the answer.",
    )
    conversation_id: int | None = Field(
        default=None,
        description="Id of the conversation this turn was persisted to.",
    )
    message_id: int | None = Field(
        default=None,
        description="Id of the persisted assistant message for this turn.",
    )
