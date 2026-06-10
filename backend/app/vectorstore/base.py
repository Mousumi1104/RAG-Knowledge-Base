"""Abstract vector store interface.

Concrete stores (pgvector) implement this so the rest of the system depends only
on the interface. Swapping backends means adding one class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypedDict


class QueryResult(TypedDict):
    """A single ranked search hit returned by :meth:`VectorStore.query`."""

    id: str
    score: float
    metadata: dict[str, Any]
    document: str


class VectorStore(ABC):
    """Abstract per-chunk vector store."""

    @abstractmethod
    def add(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None:
        """Upsert vectors with parallel metadata and source documents."""

    @abstractmethod
    def query(
        self,
        embedding: list[float],
        top_k: int,
        *,
        owner_id: int | None = None,
        include_global: bool = True,
    ) -> list[QueryResult]:
        """Return the ``top_k`` most similar records, most-relevant first.

        Tenant isolation (DEFAULT-DENY): the candidate set is restricted to
        records whose document satisfies ``owner_id == <owner_id>`` OR
        (``include_global`` and the document ``is_global``). There is NO
        "all documents" fallback: if ``owner_id is None`` and
        ``include_global is False`` the result set is empty.
        """

    def keyword_query(
        self,
        embedding: list[float],
        terms: list[str],
        top_k: int,
        *,
        owner_id: int | None = None,
        include_global: bool = True,
    ) -> list[QueryResult]:
        """Return up to ``top_k`` records that literally contain any of ``terms``.

        A lexical fallback used to rescue exact-term matches that an IDF-free
        embedder ranks far below the vector ``top_k``. Each returned hit still
        carries its real vector ``score`` (vs. ``embedding``) so callers can blend
        and threshold consistently. Default implementation returns nothing; stores
        that can do a substring scan (e.g. SQL ``ILIKE``) override this.

        The SAME tenant-isolation rule as :meth:`query` applies (DEFAULT-DENY),
        so the lexical-rescue path can never leak another tenant's chunks.
        """
        return []

    @abstractmethod
    def delete_by_document(self, document_id: int) -> None:
        """Delete all vectors whose metadata ``document_id`` matches."""

    @abstractmethod
    def count(self) -> int:
        """Return the total number of stored vectors."""

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return a small health/status dict for admin/diagnostics."""
