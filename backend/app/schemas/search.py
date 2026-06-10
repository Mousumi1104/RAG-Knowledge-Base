"""Pydantic v2 schemas for the semantic search API contract.

The response is shaped around the REQUIREMENTS example (which shows individual
hits with ``document_name``, ``excerpt`` and ``score``) but wraps them in a small
envelope that also echoes the original ``query`` for client convenience.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """Natural-language search request."""

    query: str = Field(
        ...,
        min_length=1,
        description="Natural-language query to embed and match against chunks.",
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        description="Max number of ranked results to return (defaults to settings.TOP_K).",
    )


class SearchResult(BaseModel):
    """A single ranked search hit (most-relevant first; no numeric score exposed).

    Mirrors the REQUIREMENTS Search Response (``document_name``, ``excerpt``) and
    adds traceable identifiers (``document_id``, ``chunk_id``). Results are
    tenant-scoped (own + admin-global) and, like chat sources, carry no numeric
    similarity score. Ranking is conveyed purely by list order.
    """

    document_id: int = Field(..., description="Id of the source document.")
    document_name: str = Field(..., description="Filename of the source document.")
    excerpt: str = Field(..., description="The matching chunk content (possibly truncated).")
    chunk_id: str = Field(..., description="Stable vector-store id of the matching chunk.")
    page: int | None = Field(
        default=None,
        description="1-based source page the chunk came from (null if unknown).",
    )


class SearchResponse(BaseModel):
    """Wrapped, ranked search response (most-relevant first)."""

    query: str = Field(..., description="The original query string.")
    results: list[SearchResult] = Field(
        default_factory=list, description="Ranked hits, most relevant first."
    )
