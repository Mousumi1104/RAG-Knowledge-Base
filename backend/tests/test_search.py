"""Tests for the semantic search API."""

from __future__ import annotations

from tests.conftest import LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT, upload_file


def test_search_returns_relevant_results(client) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)

    resp = client.post(
        "/search",
        json={"query": "How do I get my leave request approved?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "How do I get my leave request approved?"
    results = body["results"]
    assert len(results) > 0

    for r in results:
        assert r["document_name"] == LEAVE_POLICY_NAME
        assert isinstance(r["excerpt"], str) and r["excerpt"]
        # Results no longer carry a numeric score (M4 policy); ranking is by order.
        assert "score" not in r
        assert r["document_id"] > 0
        assert r["chunk_id"]


def test_search_respects_top_k(client) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)
    resp = client.post("/search", json={"query": "annual leave days", "top_k": 1})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) <= 1


def test_search_excludes_deleted_documents(client) -> None:
    created = upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT).json()
    doc_id = created["id"]

    # Sanity: present before deletion.
    pre = client.post("/search", json={"query": "manager approval HR portal"}).json()
    assert any(r["document_id"] == doc_id for r in pre["results"])

    assert client.delete(f"/documents/{doc_id}").status_code == 200

    post = client.post("/search", json={"query": "manager approval HR portal"}).json()
    assert all(r["document_id"] != doc_id for r in post["results"])


def test_search_empty_query_422(client) -> None:
    resp = client.post("/search", json={"query": ""})
    assert resp.status_code == 422
