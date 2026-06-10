"""Tests for the documents API: upload, list, get, soft-delete, reindex."""

from __future__ import annotations

from tests.conftest import upload_file


def test_upload_txt_indexes(client) -> None:
    resp = upload_file(client, "notes.txt", "The quick brown fox jumps over the lazy dog.")
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["filename"] == "notes.txt"
    assert body["document_type"] == "txt"
    assert body["status"] == 1  # ACTIVE
    assert body["size"] > 0
    # Indexing runs synchronously on upload -> should end up 'indexed'.
    assert body["indexing_status"] == "indexed"
    assert body["chunk_count"] > 0


def test_upload_md_indexes(client) -> None:
    resp = upload_file(
        client,
        "guide.md",
        "# Title\n\nThis is a markdown document about onboarding new engineers.",
        content_type="text/markdown",
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["document_type"] == "md"
    assert body["indexing_status"] == "indexed"
    assert body["chunk_count"] > 0


def test_list_only_active(client) -> None:
    a = upload_file(client, "a.txt", "Alpha content one.").json()
    b = upload_file(client, "b.txt", "Bravo content two.").json()

    resp = client.get("/documents")
    assert resp.status_code == 200
    listing = resp.json()
    ids = {item["id"] for item in listing["items"]}
    assert {a["id"], b["id"]} <= ids
    assert listing["total"] == len(listing["items"])

    # Soft-delete one; it must drop out of the active listing.
    assert client.delete(f"/documents/{a['id']}").status_code == 200
    resp2 = client.get("/documents")
    ids2 = {item["id"] for item in resp2.json()["items"]}
    assert a["id"] not in ids2
    assert b["id"] in ids2


def test_get_by_id(client) -> None:
    created = upload_file(client, "single.txt", "Just one document here.").json()
    resp = client.get(f"/documents/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]
    assert resp.json()["filename"] == "single.txt"


def test_get_missing_404(client) -> None:
    resp = client.get("/documents/999999")
    assert resp.status_code == 404
    assert "detail" in resp.json()


def test_soft_delete_sets_status_and_404s(client) -> None:
    created = upload_file(client, "todelete.txt", "Soon to be deleted.").json()
    doc_id = created["id"]

    del_resp = client.delete(f"/documents/{doc_id}")
    assert del_resp.status_code == 200
    assert "detail" in del_resp.json()

    # Deleted doc must no longer be fetchable (active-only) -> 404.
    assert client.get(f"/documents/{doc_id}").status_code == 404

    # And it must not appear in the active listing.
    listing = client.get("/documents").json()
    assert all(item["id"] != doc_id for item in listing["items"])

    # But with include_inactive it is visible with status DELETED (5).
    inactive = client.get("/documents", params={"include_inactive": True}).json()
    match = [item for item in inactive["items"] if item["id"] == doc_id]
    assert len(match) == 1
    assert match[0]["status"] == 5  # DELETED


def test_reindex_endpoint(client) -> None:
    created = upload_file(client, "reidx.txt", "Reindex me please, with content.").json()
    doc_id = created["id"]

    resp = client.post(f"/documents/{doc_id}/reindex")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == doc_id
    assert body["indexing_status"] == "indexed"
    assert body["detail"]


def test_unsupported_extension_415(client) -> None:
    resp = upload_file(client, "evil.exe", "binary-ish", content_type="application/octet-stream")
    assert resp.status_code == 415
    assert "detail" in resp.json()


def test_empty_file_rejected(client) -> None:
    resp = upload_file(client, "empty.txt", "")
    # Empty upload is a domain ValidationError -> 422.
    assert resp.status_code == 422
