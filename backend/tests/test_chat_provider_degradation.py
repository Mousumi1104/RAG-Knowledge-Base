"""Graceful-degradation tests for provider errors / quota exhaustion.

When the retrieval embedding provider raises (``RateLimitError`` on a persistent
429, or ``ProviderError`` on a 5xx), the chat endpoints must still return a
*rendered, relevant* answer — HTTP 200 for ``/chat/query`` and a normal
``delta``+``done`` SSE pair for ``/chat/stream`` — rather than a 429/502 or a
bare ``error`` frame the client ignores (which showed a blank screen). Runs
against the local providers + Postgres test DB from ``conftest.py``.
"""

from __future__ import annotations

import json

from app.services import chat_service
from app.services.chat_service import _SERVICE_BUSY_ANSWER
from app.utils.errors import ProviderError, RateLimitError
from tests.conftest import LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT, upload_file


def _raise_rate_limit(*_args, **_kwargs):
    raise RateLimitError("Gemini embeddings rate limit exceeded")


def _raise_provider(*_args, **_kwargs):
    raise ProviderError("Gemini embeddings error (status 503)")


def _parse_sse(lines) -> list[dict]:
    frames: list[dict] = []
    for line in lines:
        if line and line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload:
                frames.append(json.loads(payload))
    return frames


def test_query_returns_graceful_message_on_rate_limit(client, monkeypatch) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)
    monkeypatch.setattr(chat_service, "_retrieve_and_build_prompt", _raise_rate_limit)

    resp = client.post("/chat/query", json={"query": "How do I request leave?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == _SERVICE_BUSY_ANSWER
    assert body["sources"] == []
    # The turn is still persisted so the conversation stays coherent.
    assert isinstance(body["conversation_id"], int) and body["conversation_id"] > 0
    assert isinstance(body["message_id"], int) and body["message_id"] > 0


def test_query_returns_graceful_message_on_provider_error(client, monkeypatch) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)
    monkeypatch.setattr(chat_service, "_retrieve_and_build_prompt", _raise_provider)

    resp = client.post("/chat/query", json={"query": "How do I request leave?"})

    assert resp.status_code == 200
    assert resp.json()["answer"] == _SERVICE_BUSY_ANSWER


def test_stream_returns_graceful_message_on_rate_limit(client, monkeypatch) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)
    monkeypatch.setattr(chat_service, "_retrieve_and_build_prompt", _raise_rate_limit)

    with client.stream("POST", "/chat/stream", json={"query": "How do I request leave?"}) as r:
        assert r.status_code == 200
        frames = _parse_sse([line for line in r.iter_lines()])

    # No bare error frame; the message renders through the normal delta path.
    assert not [f for f in frames if "error" in f]
    delta_text = "".join(f["delta"] for f in frames if "delta" in f)
    assert delta_text == _SERVICE_BUSY_ANSWER
    done = [f for f in frames if f.get("done") is True]
    assert len(done) == 1
    assert done[0]["sources"] == []


def test_employee_query_returns_graceful_message_on_rate_limit(client, monkeypatch) -> None:
    """The Employee Zone endpoint (global-only scope) degrades the same way."""
    monkeypatch.setattr(chat_service, "_retrieve_and_build_prompt", _raise_rate_limit)

    resp = client.post(
        "/employee/chat/query",
        json={"query": "What is the leave policy?"},
        headers={"X-Employee-Email": "emp@example.com", "X-Employee-Id": "E1"},
    )

    assert resp.status_code == 200
    assert resp.json()["answer"] == _SERVICE_BUSY_ANSWER
