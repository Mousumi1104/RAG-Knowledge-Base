"""Tests for the admin API: health, stats, indexing-status, reindex."""

from __future__ import annotations

from tests.conftest import LEAVE_POLICY_TEXT, upload_file


def test_admin_health_ok(client) -> None:
    resp = client.get("/admin/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["database"] == "ok"
    assert body["status"] == "ok"
    assert body["vector_db"].get("status") == "ok"


def test_admin_stats_reflects_documents(client) -> None:
    upload_file(client, "one.txt", LEAVE_POLICY_TEXT)
    upload_file(client, "two.txt", "Another short document about policies.")

    resp = client.get("/admin/stats")
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["total_documents"] == 2
    assert stats["total_chunks"] > 0
    assert stats["indexed"] == 2
    assert stats["failed"] == 0


def test_admin_stats_excludes_deleted(client) -> None:
    keep = upload_file(client, "keep.txt", LEAVE_POLICY_TEXT).json()
    drop = upload_file(client, "drop.txt", "Will be deleted shortly.").json()

    before = client.get("/admin/stats").json()
    assert before["total_documents"] == 2

    assert client.delete(f"/documents/{drop['id']}").status_code == 200

    after = client.get("/admin/stats").json()
    assert after["total_documents"] == 1
    assert after["indexed"] == 1
    # The remaining doc's chunks are still counted; the deleted doc's are not.
    # Indexing runs as a background task, so the upload response still reports
    # chunk_count=0 — re-fetch the document for its post-indexing count.
    keep_chunks = client.get(f"/documents/{keep['id']}").json()["chunk_count"]
    assert keep_chunks > 0
    assert after["total_chunks"] == keep_chunks


def test_admin_indexing_status(client) -> None:
    created = upload_file(client, "idx.txt", LEAVE_POLICY_TEXT).json()
    resp = client.get("/admin/indexing-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_documents"] == 1
    assert body["counts"].get("indexed") == 1
    docs = body["documents"]
    assert len(docs) == 1
    assert docs[0]["id"] == created["id"]
    assert docs[0]["indexing_status"] == "indexed"
    assert docs[0]["chunk_count"] > 0


def test_admin_reindex_all(client) -> None:
    a = upload_file(client, "a.txt", LEAVE_POLICY_TEXT).json()
    b = upload_file(client, "b.txt", "Second document content for reindex.").json()

    resp = client.post("/admin/reindex")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert set(body["reindexed"]) == {a["id"], b["id"]}
    assert body["failed"] == []


def test_admin_reindex_single(client) -> None:
    created = upload_file(client, "single.txt", LEAVE_POLICY_TEXT).json()
    resp = client.post("/admin/reindex", params={"document_id": created["id"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reindexed"] == [created["id"]]
    assert body["count"] == 1
