"""Tests for the chat streaming + persistence + conversation endpoints.

Exercises the new SSE ``/chat/stream`` endpoint, conversation persistence and
listing, continuation of a conversation, soft-delete, and the back-compat
``/chat/query`` contract. Everything runs against the local providers and the
Postgres test DB wired up in ``conftest.py`` (deterministic, no network/keys).
"""

from __future__ import annotations

import json

from tests.conftest import LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT, upload_file


def _parse_sse(lines) -> list[dict]:
    """Parse ``data:`` SSE frames from streamed lines into JSON dicts."""
    frames: list[dict] = []
    for line in lines:
        if not line:
            continue
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload:
                frames.append(json.loads(payload))
    return frames


def _stream(client, payload: dict) -> tuple[list[dict], str]:
    """POST /chat/stream and return (parsed_frames, content_type)."""
    with client.stream("POST", "/chat/stream", json=payload) as r:
        assert r.status_code == 200
        content_type = r.headers.get("content-type", "")
        lines = [line for line in r.iter_lines()]
    return _parse_sse(lines), content_type


# ---------------------------------------------------------------------------
# 1) SSE streaming
# ---------------------------------------------------------------------------
def test_stream_yields_deltas_and_done_frame(client) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)

    frames, content_type = _stream(
        client,
        {"query": "How do I request leave and get it approved?"},
    )

    assert "text/event-stream" in content_type

    delta_frames = [f for f in frames if "delta" in f]
    done_frames = [f for f in frames if f.get("done") is True]
    error_frames = [f for f in frames if "error" in f]

    assert not error_frames, f"unexpected error frame(s): {error_frames}"
    # Multiple delta frames whose contents concatenate into a non-empty answer.
    assert len(delta_frames) >= 1
    answer = "".join(f["delta"] for f in delta_frames)
    assert answer.strip()
    # Local extractive provider quotes context verbatim -> answer is grounded.
    assert ("HR portal" in answer) or ("approval" in answer)

    # Exactly one terminal done frame with the persistence/sources fields.
    assert len(done_frames) == 1
    done = done_frames[0]
    assert done["done"] is True
    assert isinstance(done["conversation_id"], int) and done["conversation_id"] > 0
    assert isinstance(done["message_id"], int) and done["message_id"] > 0
    assert isinstance(done["sources"], list) and len(done["sources"]) > 0
    for s in done["sources"]:
        assert s["document"] == LEAVE_POLICY_NAME
        assert isinstance(s["excerpt"], str) and s["excerpt"]
        assert s["document_id"] > 0
        # Sources no longer carry a numeric score (M4 source-reference policy).
        assert "score" not in s


# ---------------------------------------------------------------------------
# 2) Persistence
# ---------------------------------------------------------------------------
def test_stream_persists_conversation_and_messages(client) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)

    frames, _ = _stream(client, {"query": "How do I request leave?"})
    done = next(f for f in frames if f.get("done") is True)
    conv_id = done["conversation_id"]

    # The conversation is listed (newest first).
    list_resp = client.get("/chat/conversations")
    assert list_resp.status_code == 200
    listing = list_resp.json()
    assert listing["total"] >= 1
    ids = [c["id"] for c in listing["items"]]
    assert conv_id in ids
    # Newest first: our just-created conversation should be at the front.
    assert listing["items"][0]["id"] == conv_id

    # Detail returns exactly 2 messages in order (user then assistant).
    detail_resp = client.get(f"/chat/conversations/{conv_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["id"] == conv_id
    messages = detail["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[0]["content"].strip()
    assert messages[1]["content"].strip()
    assert messages[1]["sources"] is not None and len(messages[1]["sources"]) > 0


# ---------------------------------------------------------------------------
# 3) Continue conversation
# ---------------------------------------------------------------------------
def test_stream_continue_conversation(client) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)

    frames, _ = _stream(client, {"query": "How do I request leave?"})
    conv_id = next(f for f in frames if f.get("done") is True)["conversation_id"]

    # Continue the same conversation.
    frames2, _ = _stream(
        client,
        {"query": "Does it need approval?", "conversation_id": conv_id},
    )
    done2 = next(f for f in frames2 if f.get("done") is True)
    assert done2["conversation_id"] == conv_id

    detail = client.get(f"/chat/conversations/{conv_id}").json()
    messages = detail["messages"]
    assert len(messages) == 4
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant"]


# ---------------------------------------------------------------------------
# 4) Soft delete
# ---------------------------------------------------------------------------
def test_soft_delete_conversation(client) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)

    frames, _ = _stream(client, {"query": "How do I request leave?"})
    conv_id = next(f for f in frames if f.get("done") is True)["conversation_id"]

    # Sanity: present before delete.
    assert conv_id in [c["id"] for c in client.get("/chat/conversations").json()["items"]]

    del_resp = client.delete(f"/chat/conversations/{conv_id}")
    assert del_resp.status_code == 200

    # No longer listed.
    ids_after = [c["id"] for c in client.get("/chat/conversations").json()["items"]]
    assert conv_id not in ids_after

    # Detail now 404s.
    assert client.get(f"/chat/conversations/{conv_id}").status_code == 404


# ---------------------------------------------------------------------------
# 5) Back-compat: /chat/query persists + returns ids
# ---------------------------------------------------------------------------
def test_query_back_compat_returns_ids_and_persists(client) -> None:
    upload_file(client, LEAVE_POLICY_NAME, LEAVE_POLICY_TEXT)

    resp = client.post(
        "/chat/query",
        json={"query": "How do I request leave and get it approved?"},
    )
    assert resp.status_code == 200
    body = resp.json()

    # Original contract preserved.
    assert isinstance(body["answer"], str) and body["answer"].strip()
    assert isinstance(body["sources"], list) and len(body["sources"]) > 0

    # New persistence fields.
    conv_id = body["conversation_id"]
    assert isinstance(conv_id, int) and conv_id > 0
    assert isinstance(body["message_id"], int) and body["message_id"] > 0

    # A conversation + 2 messages were persisted.
    detail = client.get(f"/chat/conversations/{conv_id}").json()
    messages = detail["messages"]
    assert len(messages) == 2
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"].strip()


# ---------------------------------------------------------------------------
# 6) Validation: empty query -> 422 on both endpoints
# ---------------------------------------------------------------------------
def test_empty_query_422_on_query(client) -> None:
    assert client.post("/chat/query", json={"query": ""}).status_code == 422


def test_empty_query_422_on_stream(client) -> None:
    with client.stream("POST", "/chat/stream", json={"query": ""}) as r:
        assert r.status_code == 422
