"""Tests for the indexing pipeline and vector lifecycle."""

from __future__ import annotations

from sqlalchemy import select

from app.database.base import Status
from app.models.chunk import Chunk
from app.services import search_service
from tests.conftest import LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT, upload_file


def test_upload_creates_active_chunks(client, db) -> None:
    created = upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT).json()
    doc_id = created["id"]

    # Indexing runs in a background task; re-fetch to observe the final state.
    fetched = client.get(f"/documents/{doc_id}").json()
    assert fetched["indexing_status"] == "indexed"
    assert fetched["chunk_count"] > 0

    chunks = db.execute(select(Chunk).where(Chunk.document_id == doc_id)).scalars().all()
    assert len(chunks) == fetched["chunk_count"]
    assert all(c.status == Status.ACTIVE.value for c in chunks)
    assert all(c.content for c in chunks)
    # chunk_id values are unique and stable.
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_reindex_keeps_indexed(client, db) -> None:
    created = upload_file(client, "reidx.txt", LEAVE_POLICY_TEXT).json()
    doc_id = created["id"]

    resp = client.post(f"/documents/{doc_id}/reindex")
    assert resp.status_code == 200
    # Reindex is queued in the background; re-fetch to observe the final state.
    assert client.get(f"/documents/{doc_id}").json()["indexing_status"] == "indexed"

    chunks = (
        db.execute(
            select(Chunk).where(
                Chunk.document_id == doc_id,
                Chunk.status == Status.ACTIVE.value,
            )
        )
        .scalars()
        .all()
    )
    assert len(chunks) > 0


def test_delete_removes_vectors(client, db) -> None:
    created = upload_file(client, "vectors.txt", LEAVE_POLICY_TEXT).json()
    doc_id = created["id"]

    # Before delete: search finds the document.
    pre = client.post("/search", json={"query": "leave approval HR portal"}).json()
    assert any(r["document_id"] == doc_id for r in pre["results"])

    # Vectors are present in the store.
    store = search_service._get_vector_store()
    assert store.count() > 0

    assert client.delete(f"/documents/{doc_id}").status_code == 200

    # Chunk rows soft-deleted.
    active_chunks = (
        db.execute(
            select(Chunk).where(
                Chunk.document_id == doc_id,
                Chunk.status == Status.ACTIVE.value,
            )
        )
        .scalars()
        .all()
    )
    assert active_chunks == []

    # Vectors for the document removed from the store.
    assert store.count() == 0

    # And search no longer returns the deleted document.
    post = client.post("/search", json={"query": "leave approval HR portal"}).json()
    assert all(r["document_id"] != doc_id for r in post["results"])
