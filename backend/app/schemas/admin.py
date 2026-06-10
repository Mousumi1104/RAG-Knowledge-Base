"""Pydantic v2 response schemas for the admin API.

Several fields are intentionally typed as ``dict``/dynamic where the underlying
structure is provider-defined (e.g. vector-store health) so the schemas stay
faithful to the service outputs without over-constraining them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StatsResponse(BaseModel):
    """Aggregate stats over ACTIVE documents and the vector store."""

    total_documents: int = Field(..., description="Count of ACTIVE documents.")
    total_chunks: int = Field(..., description="Count of ACTIVE chunks.")
    indexed: int = Field(0, description="ACTIVE documents with status 'indexed'.")
    failed: int = Field(0, description="ACTIVE documents with status 'failed'.")
    pending: int = Field(0, description="ACTIVE documents with status 'pending'.")
    processing: int = Field(0, description="ACTIVE documents with status 'processing'.")
    vector_db_health: dict[str, Any] = Field(
        default_factory=dict, description="Vector store health/status payload."
    )
    vector_count: int | None = Field(
        None, description="Total stored vectors (None if store unavailable)."
    )


class DocumentIndexingStatus(BaseModel):
    """Per-document indexing detail."""

    id: int
    filename: str
    indexing_status: str
    chunk_count: int
    error: str | None = None


class IndexingStatusResponse(BaseModel):
    """Breakdown of ACTIVE documents by indexing status, with per-doc detail."""

    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Document counts keyed by indexing_status.",
    )
    total_documents: int = Field(..., description="Total ACTIVE documents.")
    documents: list[DocumentIndexingStatus] = Field(
        default_factory=list, description="Per-document indexing detail."
    )


class ReindexRequest(BaseModel):
    """Optional reindex request body."""

    document_id: int | None = Field(
        None, description="Document id to reindex; if omitted, reindex all."
    )


class ReindexResponse(BaseModel):
    """Summary of a (re)index operation."""

    reindexed: list[int] = Field(default_factory=list, description="Ids successfully reindexed.")
    failed: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Failures as {id, error} entries.",
    )
    count: int = Field(..., description="Number of documents successfully reindexed.")


class HealthResponse(BaseModel):
    """Admin health probe result."""

    database: str = Field(..., description="'ok' or an error description.")
    vector_db: dict[str, Any] = Field(
        default_factory=dict, description="Vector store health payload."
    )
    status: str = Field(..., description="'ok' or 'degraded'.")
