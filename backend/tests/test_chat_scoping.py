"""Milestone 5 — chat conversation per-user scoping / tenant isolation tests.

Asserts (all enforced server-side at the query level, never trusting the client):
- Every conversation is created owned by its creator (``user_id``).
- A fresh conversation is started per the auto-new behavior: an explicit
  ``POST /chat/conversations`` returns a unique id, and ``/chat/query`` /
  ``/chat/stream`` auto-create a new conversation when no ``conversation_id`` is
  supplied.
- User B can NOT list / get / read-messages / delete User A's conversation; a
  foreign id 404s identically to a missing one (no existence leak).
- Messages are appended only to the caller's own conversation: continuing with a
  foreign ``conversation_id`` never attaches to that thread; it forks a new one
  owned by the caller.
- All ``/chat/*`` routes require authentication (401 without a token).

Runs against the local providers + Postgres test DB from ``conftest.py``. Each
helper uses a unique email so users (which persist across the per-test data
truncation of chunks/documents) never collide.
"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

from app.main import app


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def _register_and_token(client: TestClient, email: str, password: str = "supersecret123") -> str:
    reg = client.post("/auth/register", json={"email": email, "password": password})
    assert reg.status_code == 201, reg.text
    login = client.post("/auth/login", data={"username": email, "password": password})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _user_client(token: str) -> TestClient:
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def _two_users(anon_client) -> tuple[TestClient, TestClient]:
    """Return (clientA, clientB) for two distinct registered users."""
    token_a = _register_and_token(anon_client, _unique_email("usera"))
    token_b = _register_and_token(anon_client, _unique_email("userb"))
    return _user_client(token_a), _user_client(token_b)


def _start_conversation_via_query(client: TestClient, query: str = "Hello there") -> int:
    """Drive a /chat/query turn and return the resolved conversation id."""
    resp = client.post("/chat/query", json={"query": query})
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["conversation_id"]
    assert isinstance(conv_id, int) and conv_id > 0
    return conv_id


# ---------------------------------------------------------------------------
# 1) Ownership on create + auto-new per session
# ---------------------------------------------------------------------------
def test_explicit_create_conversation_returns_unique_owned_id(anon_client) -> None:
    client_a, client_b = _two_users(anon_client)

    r1 = client_a.post("/chat/conversations")
    assert r1.status_code == 201, r1.text
    r2 = client_a.post("/chat/conversations")
    assert r2.status_code == 201, r2.text
    id1, id2 = r1.json()["id"], r2.json()["id"]
    assert id1 != id2  # each conversation has a unique id

    # The created conversations belong to user A only.
    assert {c["id"] for c in client_a.get("/chat/conversations").json()["items"]} >= {id1, id2}
    assert not ({c["id"] for c in client_b.get("/chat/conversations").json()["items"]} & {id1, id2})


def test_query_without_conversation_id_auto_creates(anon_client) -> None:
    client_a, _ = _two_users(anon_client)
    conv_id = _start_conversation_via_query(client_a)
    # A brand-new conversation was created and is owned by the caller.
    listed = {c["id"] for c in client_a.get("/chat/conversations").json()["items"]}
    assert conv_id in listed


# ---------------------------------------------------------------------------
# 2) Scoped history — no cross-user listing
# ---------------------------------------------------------------------------
def test_list_conversations_is_user_scoped(anon_client) -> None:
    client_a, client_b = _two_users(anon_client)

    conv_a = _start_conversation_via_query(client_a, "User A question")
    conv_b = _start_conversation_via_query(client_b, "User B question")

    a_ids = {c["id"] for c in client_a.get("/chat/conversations").json()["items"]}
    b_ids = {c["id"] for c in client_b.get("/chat/conversations").json()["items"]}

    assert conv_a in a_ids and conv_a not in b_ids
    assert conv_b in b_ids and conv_b not in a_ids


# ---------------------------------------------------------------------------
# 3) No cross-user get / messages / delete (404, no existence leak)
# ---------------------------------------------------------------------------
def test_user_b_cannot_get_user_a_conversation(anon_client) -> None:
    client_a, client_b = _two_users(anon_client)
    conv_a = _start_conversation_via_query(client_a)

    # Owner can read it; the other user gets a 404 (indistinguishable from missing).
    assert client_a.get(f"/chat/conversations/{conv_a}").status_code == 200
    assert client_b.get(f"/chat/conversations/{conv_a}").status_code == 404


def test_user_b_cannot_delete_user_a_conversation(anon_client) -> None:
    client_a, client_b = _two_users(anon_client)
    conv_a = _start_conversation_via_query(client_a)

    # B's delete attempt 404s and does NOT remove A's conversation.
    assert client_b.delete(f"/chat/conversations/{conv_a}").status_code == 404
    assert client_a.get(f"/chat/conversations/{conv_a}").status_code == 200


# ---------------------------------------------------------------------------
# 4) Messages appended only to own conversations (no attach to foreign thread)
# ---------------------------------------------------------------------------
def test_continuing_foreign_conversation_id_forks_new_owned_thread(anon_client) -> None:
    client_a, client_b = _two_users(anon_client)
    conv_a = _start_conversation_via_query(client_a, "User A first message")

    # B posts using A's conversation_id. It must NOT attach to A's thread; instead
    # a NEW conversation owned by B is created.
    resp = client_b.post(
        "/chat/query",
        json={"query": "User B trying to hijack", "conversation_id": conv_a},
    )
    assert resp.status_code == 200, resp.text
    b_conv = resp.json()["conversation_id"]
    assert b_conv != conv_a  # never the same thread

    # A's conversation is untouched: still exactly its original 2 messages.
    a_detail = client_a.get(f"/chat/conversations/{conv_a}").json()
    assert len(a_detail["messages"]) == 2

    # B's new conversation is theirs, and A cannot see it.
    assert client_b.get(f"/chat/conversations/{b_conv}").status_code == 200
    assert client_a.get(f"/chat/conversations/{b_conv}").status_code == 404


def test_stream_continuing_foreign_conversation_id_forks_new_thread(anon_client) -> None:
    client_a, client_b = _two_users(anon_client)
    conv_a = _start_conversation_via_query(client_a, "User A streamed message")

    with client_b.stream(
        "POST",
        "/chat/stream",
        json={"query": "B hijack via stream", "conversation_id": conv_a},
    ) as r:
        assert r.status_code == 200
        lines = list(r.iter_lines())
    done = next(
        json.loads(line[len("data:") :].strip())
        for line in lines
        if line.startswith("data:") and '"done"' in line
    )
    assert done["conversation_id"] != conv_a

    # A's thread remains unchanged (2 messages from the original turn).
    a_detail = client_a.get(f"/chat/conversations/{conv_a}").json()
    assert len(a_detail["messages"]) == 2


# ---------------------------------------------------------------------------
# 5) All chat endpoints require auth
# ---------------------------------------------------------------------------
def test_all_chat_endpoints_require_auth(anon_client) -> None:
    assert anon_client.post("/chat/conversations").status_code == 401
    assert anon_client.get("/chat/conversations").status_code == 401
    assert anon_client.get("/chat/conversations/1").status_code == 401
    assert anon_client.delete("/chat/conversations/1").status_code == 401
    assert anon_client.post("/chat/query", json={"query": "x"}).status_code == 401
    with anon_client.stream("POST", "/chat/stream", json={"query": "x"}) as r:
        assert r.status_code == 401
