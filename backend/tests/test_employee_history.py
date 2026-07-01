"""Tests for the Employee-Zone server-side chat-history stack.

Exercises the separate ``employee_conversations``/``employee_messages`` tables via
the ``/employee/chat/*`` API: auto-persist on query + stream, per-employee
isolation, list pagination + preview + message_count, get-with-messages, rename
(400 on empty), idempotent delete, created_at/updated_at JSON keys, and identity
preamble stripping. Runs against the local providers + Postgres test DB from
``conftest.py``; RAG retrieval/generation is monkeypatched for determinism.
"""

from __future__ import annotations

import json

from app.services import chat_service


def _headers(emp_id: str = "EMP-1", email: str = "e1@corp.com", name: str = "E One") -> dict:
    return {"X-Employee-Id": emp_id, "X-Employee-Email": email, "X-Employee-Name": name}


def _fake_retrieve(*_a, **_k):
    return ("SYS", "USER", [{"document": "HR.pdf", "excerpt": "ex", "document_id": 1, "page": 2}])


def _patch_rag(monkeypatch, answer: str = "The answer.") -> None:
    monkeypatch.setattr(chat_service, "_retrieve_and_build_prompt", _fake_retrieve)
    monkeypatch.setattr(chat_service, "_generate_answer", lambda system, user: answer)


def _ask(client, text: str, headers: dict, conversation_id=None) -> dict:
    resp = client.post(
        "/employee/chat/query",
        json={"query": text, "conversation_id": conversation_id},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _parse_sse(lines) -> list[dict]:
    frames = []
    for line in lines:
        if line and line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload:
                frames.append(json.loads(payload))
    return frames


# ---------------------------------------------------------------------------
def test_query_autopersists_and_lists(client, monkeypatch) -> None:
    _patch_rag(monkeypatch)
    body = _ask(client, "What is the leave policy?", _headers())
    assert body["answer"] == "The answer."
    assert body["conversation_id"] and body["message_id"]

    resp = client.get("/employee/chat/conversations", headers=_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_more"] is False and data["next_cursor"] is None
    assert len(data["conversations"]) == 1
    item = data["conversations"][0]
    assert item["title"] == "What is the leave policy?"
    assert item["message_count"] == 2
    assert item["preview"]  # snippet of the latest (assistant) message
    assert "created_at" in item and "updated_at" in item


def test_get_conversation_returns_messages_with_sources(client, monkeypatch) -> None:
    _patch_rag(monkeypatch)
    conv_id = _ask(client, "How many casual leaves?", _headers())["conversation_id"]

    resp = client.get(f"/employee/chat/conversations/{conv_id}", headers=_headers())
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["id"] == conv_id
    assert detail["message_count"] == 2
    assert "created_at" in detail and "updated_at" in detail
    msgs = detail["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "How many casual leaves?"
    assert msgs[1]["content"] == "The answer."
    assert msgs[1]["sources"][0]["document"] == "HR.pdf"
    assert "created_at" in msgs[0]


def test_isolation_between_employees(client, monkeypatch) -> None:
    _patch_rag(monkeypatch)
    a = _headers("EMP-A", "a@corp.com", "A")
    b = _headers("EMP-B", "b@corp.com", "B")
    a_conv = _ask(client, "A private question", a)["conversation_id"]
    _ask(client, "B private question", b)

    a_list = client.get("/employee/chat/conversations", headers=a).json()
    b_list = client.get("/employee/chat/conversations", headers=b).json()
    assert len(a_list["conversations"]) == 1
    assert len(b_list["conversations"]) == 1
    assert a_list["conversations"][0]["id"] != b_list["conversations"][0]["id"]

    # B cannot read A's conversation (404-mask, no existence leak).
    forbidden = client.get(f"/employee/chat/conversations/{a_conv}", headers=b)
    assert forbidden.status_code == 404


def test_rename_and_empty_title_400(client, monkeypatch) -> None:
    _patch_rag(monkeypatch)
    conv_id = _ask(client, "Original question", _headers())["conversation_id"]

    ok = client.patch(
        f"/employee/chat/conversations/{conv_id}",
        json={"title": "Renamed thread"},
        headers=_headers(),
    )
    assert ok.status_code == 200
    assert ok.json()["title"] == "Renamed thread"

    bad = client.patch(
        f"/employee/chat/conversations/{conv_id}",
        json={"title": "   "},
        headers=_headers(),
    )
    assert bad.status_code == 400


def test_delete_is_idempotent(client, monkeypatch) -> None:
    _patch_rag(monkeypatch)
    conv_id = _ask(client, "Delete me", _headers())["conversation_id"]

    d1 = client.delete(f"/employee/chat/conversations/{conv_id}", headers=_headers())
    assert d1.status_code == 200 and d1.json() == {"deleted": True, "id": conv_id}
    # Deleting again (now-gone) still succeeds — fire-and-forget cleanup.
    d2 = client.delete(f"/employee/chat/conversations/{conv_id}", headers=_headers())
    assert d2.status_code == 200

    listed = client.get("/employee/chat/conversations", headers=_headers()).json()
    assert all(c["id"] != conv_id for c in listed["conversations"])
    assert client.get(f"/employee/chat/conversations/{conv_id}", headers=_headers()).status_code == 404


def test_list_pagination_cursor(client, monkeypatch) -> None:
    _patch_rag(monkeypatch)
    h = _headers("EMP-PAGE", "page@corp.com", "Pager")
    ids = [_ask(client, f"Question {i}", h)["conversation_id"] for i in range(3)]

    page1 = client.get("/employee/chat/conversations?limit=2", headers=h).json()
    assert len(page1["conversations"]) == 2
    assert page1["has_more"] is True and page1["next_cursor"]

    page2 = client.get(
        f"/employee/chat/conversations?limit=2&cursor={page1['next_cursor']}", headers=h
    ).json()
    assert len(page2["conversations"]) == 1
    assert page2["has_more"] is False and page2["next_cursor"] is None

    seen = [c["id"] for c in page1["conversations"]] + [c["id"] for c in page2["conversations"]]
    assert sorted(seen) == sorted(ids)  # all three, no overlap
    # Newest-first: the last-created id leads page 1.
    assert page1["conversations"][0]["id"] == ids[-1]


def test_stream_autopersists(client, monkeypatch) -> None:
    monkeypatch.setattr(chat_service, "_retrieve_and_build_prompt", _fake_retrieve)
    monkeypatch.setattr(
        chat_service, "_stream_answer_deltas", lambda system, user: iter(["Hello ", "world"])
    )
    h = _headers("EMP-STREAM", "stream@corp.com", "Streamer")

    with client.stream(
        "POST", "/employee/chat/stream", json={"query": "Stream this"}, headers=h
    ) as r:
        assert r.status_code == 200
        frames = _parse_sse(list(r.iter_lines()))

    assert not [f for f in frames if "error" in f]
    assert "".join(f["delta"] for f in frames if "delta" in f) == "Hello world"
    done = [f for f in frames if f.get("done") is True]
    assert len(done) == 1 and done[0]["conversation_id"] and done[0]["message_id"]

    listed = client.get("/employee/chat/conversations", headers=h).json()
    assert len(listed["conversations"]) == 1
    assert listed["conversations"][0]["message_count"] == 2


def test_identity_preamble_stripped_from_stored_user_text(client, monkeypatch) -> None:
    _patch_rag(monkeypatch)
    q = "(For context, I am Asha from Sales.) What is the leave policy?"
    conv_id = _ask(client, q, _headers())["conversation_id"]

    detail = client.get(f"/employee/chat/conversations/{conv_id}", headers=_headers()).json()
    assert detail["messages"][0]["content"] == "What is the leave policy?"
    # The derived title is also preamble-free (reads cleanly in the history list).
    assert detail["title"] == "What is the leave policy?"
