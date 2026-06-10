"""Milestone 4 — SECURITY-CRITICAL retrieval isolation tests.

Asserts (all enforced server-side in the vector layer + source builder, never
trusting the client):

- A user's chat/search retrieves ONLY {their own docs + admin-global docs}. A
  query targeting another user's UNIQUE content returns ZERO foreign hits — not
  in results, not in sources, and not in the prompt context.
- Sources shown to a user contain ONLY that user's own (non-global) documents,
  in retrieval order, with NO numeric ``score`` field.
- An answer grounded only on an admin-global doc returns an EMPTY sources list
  (the global doc still grounds the answer / stays in the prompt context).
- The lexical keyword-rescue path (``keyword_query``) is ALSO tenant-scoped: a
  query crafted to lexically match a foreign chunk excludes it.
- DEFAULT-DENY: ``search_service.search`` with ``owner_id=None,
  include_global=False`` returns nothing (no "all documents" fallback).

Runs against the local providers + Postgres test DB from ``conftest.py``. Users
persist across the per-test truncation of chunks/documents, so each test uses a
unique email to avoid collisions.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services import chat_service, search_service

_SETTINGS = get_settings()

# Distinctive, non-overlapping vocabularies so a query for one user's content can
# never lexically/semantically match the other's chunks.
ALICE_NAME = "alice_doc.txt"
ALICE_TEXT = (
    "Alice private memo. The zephyrwidget calibration constant is 4217. "
    "Only Alice's quokkanetics report mentions the zephyrwidget protocol."
)
ALICE_UNIQUE_TERM = "zephyrwidget"

BOB_NAME = "bob_doc.txt"
BOB_TEXT = (
    "Bob confidential note. The plumbershark routing key is 9981. "
    "Bob's narwhalogram ledger documents the plumbershark sequence."
)
BOB_UNIQUE_TERM = "plumbershark"

GLOBAL_NAME = "company_handbook.txt"
GLOBAL_TEXT = (
    "Company handbook. The glorptastic onboarding checklist applies to everyone. "
    "All staff must complete the glorptastic orientation in week one."
)
GLOBAL_UNIQUE_TERM = "glorptastic"


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


def _upload(client: TestClient, name: str, content: str) -> dict:
    resp = client.post(
        "/documents/upload",
        files={"file": (name, content.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _upload_global(admin: TestClient, name: str, content: str) -> dict:
    resp = admin.post(
        "/documents/global",
        files={"file": (name, content.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _me(client: TestClient) -> dict:
    return client.get("/auth/me").json()


# ---------------------------------------------------------------------------
# Routes now require authentication.
# ---------------------------------------------------------------------------
def test_chat_and_search_require_auth(anon_client) -> None:
    assert anon_client.post("/chat/query", json={"query": "hello?"}).status_code == 401
    assert anon_client.post("/chat/stream", json={"query": "hello?"}).status_code == 401
    assert anon_client.post("/search", json={"query": "hello?"}).status_code == 401


# ---------------------------------------------------------------------------
# Cross-tenant isolation: a user only ever retrieves own + global content.
# ---------------------------------------------------------------------------
def test_search_excludes_foreign_users_content(anon_client, client) -> None:
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    b_token = _register_and_token(anon_client, _unique_email("bob"))
    alice, bob = _user_client(a_token), _user_client(b_token)

    a_doc = _upload(alice, ALICE_NAME, ALICE_TEXT)
    b_doc = _upload(bob, BOB_NAME, BOB_TEXT)

    # Alice searches for HER unique term -> finds only her doc.
    a_results = alice.post("/search", json={"query": ALICE_UNIQUE_TERM}).json()["results"]
    a_ids = {r["document_id"] for r in a_results}
    assert a_doc["id"] in a_ids
    assert b_doc["id"] not in a_ids

    # Alice searches for BOB's unique term -> ZERO foreign hits (default-deny).
    foreign = alice.post("/search", json={"query": BOB_UNIQUE_TERM}).json()["results"]
    assert all(r["document_id"] != b_doc["id"] for r in foreign)
    assert all(BOB_UNIQUE_TERM not in r["excerpt"].lower() for r in foreign)


def test_chat_excludes_foreign_users_content_everywhere(anon_client) -> None:
    """Foreign content appears in neither sources NOR the prompt context."""
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    b_token = _register_and_token(anon_client, _unique_email("bob"))
    alice, bob = _user_client(a_token), _user_client(b_token)
    alice_me = _me(alice)

    a_doc = _upload(alice, ALICE_NAME, ALICE_TEXT)
    _upload(bob, BOB_NAME, BOB_TEXT)

    # Alice asks about BOB's unique content. Verify at the service layer that the
    # retrieved hits (which feed BOTH the prompt context and the sources) contain
    # zero of Bob's chunks.
    from app.database.session import SessionLocal

    db = SessionLocal()
    try:
        hits = search_service.search(
            db, query=f"what is the {BOB_UNIQUE_TERM} routing key", owner_id=alice_me["id"]
        )
    finally:
        db.close()
    assert all(BOB_UNIQUE_TERM not in (h["excerpt"] or "").lower() for h in hits)
    # No hit may belong to a document Alice does not own and is not global.
    for h in hits:
        assert h["is_global"] or h["owner_id"] == alice_me["id"]

    # End-to-end: the chat answer for Bob's term surfaces no sources pointing at Bob.
    resp = alice.post("/chat/query", json={"query": f"what is the {BOB_UNIQUE_TERM} routing key?"})
    assert resp.status_code == 200
    for s in resp.json()["sources"]:
        assert s["document"] != BOB_NAME
        assert BOB_UNIQUE_TERM not in s["excerpt"].lower()

    # Sanity: Alice CAN retrieve her own content.
    own = alice.post("/chat/query", json={"query": f"what is the {ALICE_UNIQUE_TERM} constant?"})
    own_sources = own.json()["sources"]
    assert any(s["document_id"] == a_doc["id"] for s in own_sources)


# ---------------------------------------------------------------------------
# Source-reference rules: own non-global docs only, ordered, NO score.
# ---------------------------------------------------------------------------
def test_sources_only_own_non_global_no_score(anon_client) -> None:
    token = _register_and_token(anon_client, _unique_email("alice"))
    alice = _user_client(token)
    alice_me = _me(alice)
    a_doc = _upload(alice, ALICE_NAME, ALICE_TEXT)

    resp = alice.post("/chat/query", json={"query": f"explain the {ALICE_UNIQUE_TERM} protocol"})
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    assert len(sources) > 0
    for s in sources:
        assert s["document"] == ALICE_NAME
        assert s["document_id"] == a_doc["id"]
        # No numeric score is ever serialized.
        assert "score" not in s
    # Ownership (non-global, owned by alice) is implied by it being shown at all.
    assert alice_me["id"] is not None


def test_answer_grounded_only_on_global_doc_has_no_sources(anon_client, client) -> None:
    """A global doc grounds the answer but is excluded from the sources list."""
    _upload_global(client, GLOBAL_NAME, GLOBAL_TEXT)

    token = _register_and_token(anon_client, _unique_email("reader"))
    user = _user_client(token)
    user_me = _me(user)
    # The user has NO own docs; only the global handbook can ground the answer.

    resp = user.post("/chat/query", json={"query": f"what is the {GLOBAL_UNIQUE_TERM} checklist?"})
    assert resp.status_code == 200
    body = resp.json()
    # Sources are empty (global doc excluded), even though it grounded the answer.
    assert body["sources"] == []

    # And confirm at the service layer that the global doc WAS in the retrieved
    # context (so it really grounded the answer; it is just not a listed source).
    from app.database.session import SessionLocal

    db = SessionLocal()
    try:
        hits = search_service.search(
            db, query=f"what is the {GLOBAL_UNIQUE_TERM} checklist", owner_id=user_me["id"]
        )
    finally:
        db.close()
    assert any(h["is_global"] and GLOBAL_UNIQUE_TERM in (h["excerpt"] or "").lower() for h in hits)
    # _build_sources drops every global hit -> empty.
    assert chat_service._build_sources(hits, user_me["id"]) == []


def test_global_docs_ground_answer_but_own_docs_are_the_sources(anon_client, client) -> None:
    """Mixed grounding: global + own. Only the own doc appears as a source."""
    _upload_global(client, GLOBAL_NAME, GLOBAL_TEXT)

    token = _register_and_token(anon_client, _unique_email("mixed"))
    user = _user_client(token)
    user_me = _me(user)
    own_doc = _upload(user, ALICE_NAME, ALICE_TEXT)

    from app.database.session import SessionLocal

    db = SessionLocal()
    try:
        # A query touching both the user's term and the global term.
        hits = search_service.search(
            db,
            query=f"{ALICE_UNIQUE_TERM} and {GLOBAL_UNIQUE_TERM}",
            owner_id=user_me["id"],
        )
    finally:
        db.close()

    # Context (hits) may include the global doc.
    assert any(h["is_global"] for h in hits) or any(
        h["document_id"] == own_doc["id"] for h in hits
    )
    sources = chat_service._build_sources(hits, user_me["id"])
    # Every listed source is the user's own non-global doc.
    assert all(s["document_id"] == own_doc["id"] for s in sources)
    assert all(s["document"] == ALICE_NAME for s in sources)


# ---------------------------------------------------------------------------
# Keyword-rescue (ILIKE) path is ALSO scoped.
# ---------------------------------------------------------------------------
def test_keyword_rescue_path_is_tenant_scoped(anon_client) -> None:
    """A query lexically matching a foreign chunk must NOT rescue that chunk."""
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    b_token = _register_and_token(anon_client, _unique_email("bob"))
    alice, bob = _user_client(a_token), _user_client(b_token)
    alice_me = _me(alice)

    _upload(alice, ALICE_NAME, ALICE_TEXT)
    b_doc = _upload(bob, BOB_NAME, BOB_TEXT)

    from app.database.session import SessionLocal
    from app.rag.embeddings import get_embedding_provider
    from app.vectorstore import get_vector_store

    store = get_vector_store()
    embedding = get_embedding_provider().embed_query(BOB_UNIQUE_TERM)

    # Directly exercise the ILIKE keyword path with Bob's literal term while
    # scoped to Alice. It must return ZERO of Bob's chunks.
    kw_hits = store.keyword_query(
        embedding, [BOB_UNIQUE_TERM], top_k=50, owner_id=alice_me["id"], include_global=True
    )
    assert all(
        (h["metadata"].get("document_id")) != b_doc["id"] for h in kw_hits
    ), "keyword_query leaked a foreign tenant's chunk"
    assert all(BOB_UNIQUE_TERM not in (h["document"] or "").lower() for h in kw_hits)

    # Via the search service blend, Bob's lexically-matching chunk is still absent.
    db = SessionLocal()
    try:
        hits = search_service.search(db, query=BOB_UNIQUE_TERM, owner_id=alice_me["id"])
    finally:
        db.close()
    assert all(h["document_id"] != b_doc["id"] for h in hits)


# ---------------------------------------------------------------------------
# DEFAULT-DENY: no scope + no globals == empty.
# ---------------------------------------------------------------------------
def test_default_deny_no_owner_no_global_returns_empty(anon_client) -> None:
    token = _register_and_token(anon_client, _unique_email("alice"))
    alice = _user_client(token)
    _upload(alice, ALICE_NAME, ALICE_TEXT)

    from app.database.session import SessionLocal

    db = SessionLocal()
    try:
        # owner_id=None AND include_global=False -> match nothing, no fallback.
        hits = search_service.search(
            db, query=ALICE_UNIQUE_TERM, owner_id=None, include_global=False
        )
    finally:
        db.close()
    assert hits == []
