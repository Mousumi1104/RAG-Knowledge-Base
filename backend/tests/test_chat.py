"""Tests for the RAG chat API."""

from __future__ import annotations

from app.services import chat_service
from tests.conftest import LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT, upload_file


def test_chat_grounded_answer_with_sources(client) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)

    resp = client.post(
        "/chat/query",
        json={"query": "How do I request leave and get it approved?"},
    )
    assert resp.status_code == 200
    body = resp.json()

    answer = body["answer"]
    assert isinstance(answer, str) and answer.strip()
    # Local extractive provider quotes context verbatim -> answer is grounded.
    assert ("HR portal" in answer) or ("approval" in answer)

    sources = body["sources"]
    assert len(sources) > 0
    for s in sources:
        assert s["document"] == LEAVE_POLICY_NAME
        assert isinstance(s["excerpt"], str) and s["excerpt"]
        assert s["document_id"] > 0
        # Sources no longer carry a numeric score (M4 source-reference policy).
        assert "score" not in s


def test_chat_empty_query_422(client) -> None:
    resp = client.post("/chat/query", json={"query": ""})
    assert resp.status_code == 422


def test_chat_empty_knowledge_base(client) -> None:
    # No documents uploaded in this isolated test.
    resp = client.post("/chat/query", json={"query": "What is the leave policy?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"] == []
    # A non-empty, graceful "not found" answer (never a 500).
    assert isinstance(body["answer"], str) and body["answer"].strip()


def test_is_no_answer_helper() -> None:
    assert chat_service._is_no_answer(
        "I couldn't find relevant information in the knowledge base to answer your question."
    )
    assert chat_service._is_no_answer(
        "I could not find the information in the knowledge base."
    )
    assert chat_service._is_no_answer("No relevant information was found.")
    # A normal grounded answer must not be misclassified as a disclaimer.
    assert not chat_service._is_no_answer(
        "Submit your leave request via the HR portal; manager approval is required."
    )


def test_chat_no_answer_disclaimer_suppresses_sources(client, monkeypatch) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)

    disclaimer = (
        "I couldn't find relevant information in the knowledge base to answer your question."
    )
    monkeypatch.setattr(
        chat_service, "_generate_answer", lambda system, user: disclaimer
    )

    resp = client.post(
        "/chat/query",
        json={"query": "How do I request leave and get it approved?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Even though retrieval found hits, the disclaimer answer suppresses sources.
    assert body["sources"] == []
    assert disclaimer in body["answer"]
