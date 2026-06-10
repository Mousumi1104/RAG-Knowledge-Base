"""End-to-end multi-tenant isolation security test (adversarial).

Builds a 3-actor scenario entirely through the public HTTP API (admin + userA +
userB) and asserts every cross-tenant boundary the app promises, end to end:

1.  Doc listing scope (own-only; admin sees all; library excludes globals).
2.  IDOR on /documents/{id}, DELETE, /reindex, /file -> 404 (no existence leak).
3.  Retrieval isolation (vector + keyword paths) for /chat/query and /search.
4.  Source-reference rules (own non-global only, no score, global -> empty).
5.  Chat-history scoping (no cross-user list/read/delete; unique owned ids).
6.  Disabled user can no longer log in AND an existing token is rejected.
7.  No metadata leak (no foreign email/filename/owner; never password_hash).

Runs against the local providers + Postgres test DB from ``conftest.py``.
TestClient runs BackgroundTasks synchronously after the response, so uploads
index inline. Each user uses a unique email (users persist across the per-test
truncation of chunks/documents).
"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

_SETTINGS = get_settings()

# Distinctive, non-overlapping vocabularies so a query for one actor's content
# can never lexically/semantically match another's chunks.
GLOBAL_NAME = "company_handbook.txt"
GLOBAL_TEXT = (
    "Company handbook. GLOBALSECRET zebra is the company-wide onboarding token. "
    "Every employee must acknowledge the GLOBALSECRET zebra handbook in week one."
)
GLOBAL_TERM = "globalsecret"
GLOBAL_TERM2 = "zebra"

ALPHA_NAME = "alpha_private.txt"
ALPHA_TEXT = (
    "UserA private memo. ALPHASECRET kangaroo is the project codeword. "
    "Only UserA's ALPHASECRET kangaroo dossier references this codeword."
)
ALPHA_TERM = "alphasecret"
ALPHA_TERM2 = "kangaroo"

BETA_NAME = "beta_private.txt"
BETA_TEXT = (
    "UserB confidential note. BETASECRET platypus is the secret routing phrase. "
    "Only UserB's BETASECRET platypus ledger documents this phrase."
)
BETA_TERM = "betasecret"
BETA_TERM2 = "platypus"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def _client_for(token: str) -> TestClient:
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def _admin_token(anon: TestClient) -> str:
    resp = anon.post(
        "/auth/login",
        data={"username": _SETTINGS.ADMIN_EMAIL, "password": _SETTINGS.ADMIN_PASSWORD},
    )
    assert resp.status_code == 200, f"admin login failed: {resp.status_code} {resp.text}"
    return resp.json()["access_token"]


def _register_login(anon: TestClient, email: str, password: str = "supersecret123") -> str:
    reg = anon.post("/auth/register", json={"email": email, "password": password})
    assert reg.status_code == 201, reg.text
    login = anon.post("/auth/login", data={"username": email, "password": password})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


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


def _contains_any(blob: str, *needles: str) -> bool:
    low = blob.lower()
    return any(n.lower() in low for n in needles)


# ---------------------------------------------------------------------------
# The one big end-to-end scenario. A single failure on any property = leak.
# ---------------------------------------------------------------------------
def test_end_to_end_multitenant_isolation(anon_client) -> None:
    # --- Build the scenario via the API --------------------------------------
    admin = _client_for(_admin_token(anon_client))
    a_email = _unique_email("usera")
    b_email = _unique_email("userb")
    a_token = _register_login(anon_client, a_email)
    b_token = _register_login(anon_client, b_email)
    userA = _client_for(a_token)
    userB = _client_for(b_token)
    a_me, b_me = _me(userA), _me(userB)

    g_doc = _upload_global(admin, GLOBAL_NAME, GLOBAL_TEXT)
    a_doc = _upload(userA, ALPHA_NAME, ALPHA_TEXT)
    b_doc = _upload(userB, BETA_NAME, BETA_TEXT)

    assert g_doc["is_global"] is True and g_doc["owner_id"] is None
    assert a_doc["owner_id"] == a_me["id"] and a_doc["is_global"] is False
    assert b_doc["owner_id"] == b_me["id"] and b_doc["is_global"] is False

    # =====================================================================
    # Property 1 — Doc listing scope
    # =====================================================================
    a_list = userA.get("/documents").json()["items"]
    a_ids = {d["id"] for d in a_list}
    assert a_doc["id"] in a_ids, "userA cannot see own doc"
    assert b_doc["id"] not in a_ids, "LEAK p1: userA sees userB's doc in listing"
    assert g_doc["id"] not in a_ids, "LEAK p1: global doc appears in personal library"
    # userA's listing leaks no foreign email/filename.
    a_list_blob = json.dumps(a_list)
    assert not _contains_any(a_list_blob, b_email, BETA_NAME), "LEAK p1: foreign metadata in listing"

    b_list = userB.get("/documents").json()["items"]
    b_ids = {d["id"] for d in b_list}
    assert b_doc["id"] in b_ids and a_doc["id"] not in b_ids
    assert g_doc["id"] not in b_ids

    admin_ids = {d["id"] for d in admin.get("/documents").json()["items"]}
    assert {a_doc["id"], b_doc["id"]} <= admin_ids, "admin should see all user docs"

    # =====================================================================
    # Property 7 (metadata, partial) — UserOut never carries password_hash
    # and userA cannot enumerate userB's metadata anywhere.
    # =====================================================================
    me_blob = json.dumps(_me(userA)) + json.dumps(_me(userB))
    assert "password_hash" not in me_blob and "password" not in me_blob, (
        "LEAK p7: password material in /auth/me"
    )
    # Admin user listing is admin-only; userA cannot list users.
    assert userA.get("/admin/users").status_code == 403, "LEAK p7: non-admin can list users"
    # Even the admin user listing must never expose password_hash.
    admin_users_resp = admin.get("/admin/users")
    assert admin_users_resp.status_code == 200
    assert "password_hash" not in admin_users_resp.text, "LEAK p7: admin users list exposes hash"

    # =====================================================================
    # Property 2 — IDOR: userA acting on userB's doc id -> 404 (== missing id)
    # =====================================================================
    missing_id = 99_000_000  # an id that certainly does not exist
    bid = b_doc["id"]
    assert userA.get(f"/documents/{bid}").status_code == 404, "LEAK p2: GET foreign doc != 404"
    assert userA.get(f"/documents/{missing_id}").status_code == 404
    assert userA.post(f"/documents/{bid}/reindex").status_code == 404, "LEAK p2: reindex foreign doc"
    assert userA.get(f"/documents/{bid}/file").status_code == 404, "LEAK p2: file-serve foreign doc"
    # No body of any IDOR 404 may leak userB's filename/email.
    for r in (
        userA.get(f"/documents/{bid}"),
        userA.post(f"/documents/{bid}/reindex"),
        userA.get(f"/documents/{bid}/file"),
    ):
        assert not _contains_any(r.text, b_email, BETA_NAME), "LEAK p2: foreign metadata in 404 body"
    # DELETE last (so the doc still exists for the retrieval checks below).
    assert userA.delete(f"/documents/{bid}").status_code == 404, "LEAK p2: DELETE foreign doc != 404"
    # The foreign doc must still be intact for its real owner.
    assert userB.get(f"/documents/{bid}").status_code == 200, "userA's DELETE damaged userB's doc"

    # =====================================================================
    # Property 3 — Retrieval isolation (vector + keyword paths), both ways.
    # =====================================================================
    # --- /search: userA targets userB's unique content (semantic phrasing) ---
    s1 = userA.post("/search", json={"query": f"what is the {BETA_TERM} {BETA_TERM2} phrase?"})
    assert s1.status_code == 200
    for r in s1.json()["results"]:
        assert r["document_id"] != bid, "LEAK p3: /search returned userB's doc to userA"
        assert not _contains_any(r["excerpt"], BETA_TERM, BETA_TERM2), "LEAK p3: betasecret in excerpt"
        assert r["document_name"] != BETA_NAME, "LEAK p3: userB filename in /search"

    # --- /search keyword/lexical path: bare literal token ---
    s2 = userA.post("/search", json={"query": BETA_TERM2})
    assert s2.status_code == 200
    for r in s2.json()["results"]:
        assert r["document_id"] != bid, "LEAK p3: keyword /search leaked userB's doc"
        assert not _contains_any(r["excerpt"], BETA_TERM, BETA_TERM2)

    # --- /chat/query: userA asks about userB's content (semantic) ---
    c1 = userA.post("/chat/query", json={"query": f"explain the {BETA_TERM} {BETA_TERM2} routing"})
    assert c1.status_code == 200
    body1 = c1.json()
    for src in body1["sources"]:
        assert src["document_id"] != bid, "LEAK p3: chat source points at userB's doc"
        assert src["document"] != BETA_NAME, "LEAK p3: userB filename in chat source"
        assert not _contains_any(src["excerpt"], BETA_TERM, BETA_TERM2), "LEAK p3: betasecret in source"
    assert not _contains_any(body1["answer"], BETA_TERM, BETA_TERM2), (
        "LEAK p3: userB's secret content surfaced in chat answer to userA"
    )

    # --- /chat/query: userA asks via lexical/keyword phrasing (bare token) ---
    c2 = userA.post("/chat/query", json={"query": BETA_TERM2})
    assert c2.status_code == 200
    body2 = c2.json()
    for src in body2["sources"]:
        assert src["document_id"] != bid and src["document"] != BETA_NAME
        assert not _contains_any(src["excerpt"], BETA_TERM, BETA_TERM2)
    assert not _contains_any(body2["answer"], BETA_TERM, BETA_TERM2), "LEAK p3: keyword chat leaked betasecret"

    # --- Reverse direction: userB cannot reach userA's ALPHASECRET ---
    for q in (f"what is the {ALPHA_TERM} {ALPHA_TERM2} codeword?", ALPHA_TERM2):
        sb = userB.post("/search", json={"query": q}).json()["results"]
        for r in sb:
            assert r["document_id"] != a_doc["id"], "LEAK p3: userB retrieved userA's doc"
            assert not _contains_any(r["excerpt"], ALPHA_TERM, ALPHA_TERM2)
        cb = userB.post("/chat/query", json={"query": q}).json()
        for src in cb["sources"]:
            assert src["document_id"] != a_doc["id"]
            assert not _contains_any(src["excerpt"], ALPHA_TERM, ALPHA_TERM2)
        assert not _contains_any(cb["answer"], ALPHA_TERM, ALPHA_TERM2), "LEAK p3: alphasecret leaked to userB"

    # =====================================================================
    # Property 4 — Source rules.
    # =====================================================================
    # (a) Own doc: sources present, reference ONLY userA's doc, no `score` field.
    own = userA.post("/chat/query", json={"query": f"explain the {ALPHA_TERM} {ALPHA_TERM2} codeword"})
    assert own.status_code == 200
    own_sources = own.json()["sources"]
    assert len(own_sources) > 0, "LEAK p4: own-doc query returned no sources"
    for src in own_sources:
        assert src["document_id"] == a_doc["id"], "LEAK p4: source not userA's own doc"
        assert src["document"] == ALPHA_NAME
        assert "score" not in src, "LEAK p4: numeric score field exposed in source"
    # Own search hits also carry no score.
    own_search = userA.post("/search", json={"query": ALPHA_TERM2}).json()["results"]
    assert all("score" not in r for r in own_search), "LEAK p4: score field in /search result"
    assert any(r["document_id"] == a_doc["id"] for r in own_search), "userA cannot find own doc"

    # (b) Global-grounded answer: answer produced, the GLOBAL doc is NEVER a
    # listed source. Use a fresh user with NO own docs so the global handbook is
    # the *sole* grounding -> sources must be EMPTY (admin-global -> no refs).
    # (For userA, who also owns an overlapping doc, we still assert the global
    # doc never leaks in as a source.)
    gq_a = userA.post("/chat/query", json={"query": f"what is {GLOBAL_TERM} {GLOBAL_TERM2}?"})
    assert gq_a.status_code == 200 and gq_a.json()["answer"]
    for src in gq_a.json()["sources"]:
        assert src["document_id"] != g_doc["id"], "LEAK p4: global doc exposed as a source"
        assert src["document"] != GLOBAL_NAME

    c_token = _register_login(anon_client, _unique_email("userc"))
    userC = _client_for(c_token)  # owns nothing; only the global doc can ground.
    gq = userC.post("/chat/query", json={"query": f"what is {GLOBAL_TERM} {GLOBAL_TERM2}?"})
    assert gq.status_code == 200
    gbody = gq.json()
    assert gbody["answer"], "global-grounded query produced no answer"
    assert gbody["sources"] == [], "LEAK p4: global-doc answer exposed a source reference"

    # =====================================================================
    # Property 5 — Chat history scoping.
    # =====================================================================
    conv_a = c1.json()["conversation_id"]  # an existing userA conversation
    assert isinstance(conv_a, int) and conv_a > 0
    # Fresh explicit conversations get unique, caller-owned ids.
    n1 = userA.post("/chat/conversations").json()["id"]
    n2 = userA.post("/chat/conversations").json()["id"]
    assert n1 != n2, "LEAK p5: duplicate conversation ids"
    a_convs = {c["id"] for c in userA.get("/chat/conversations").json()["items"]}
    assert {conv_a, n1, n2} <= a_convs

    # userB cannot list/read/delete userA's conversation (404, no existence leak).
    b_convs = {c["id"] for c in userB.get("/chat/conversations").json()["items"]}
    assert not (a_convs & b_convs), "LEAK p5: userB lists userA's conversations"
    assert userB.get(f"/chat/conversations/{conv_a}").status_code == 404, "LEAK p5: read foreign conv"
    assert userB.delete(f"/chat/conversations/{conv_a}").status_code == 404, "LEAK p5: delete foreign conv"
    assert userA.get(f"/chat/conversations/{conv_a}").status_code == 200, "userB's delete damaged userA conv"
    # Foreign conv id 404 must be indistinguishable from a missing id.
    assert userB.get(f"/chat/conversations/{99_000_000}").status_code == 404

    # =====================================================================
    # Property 7 (continued) — No metadata leak in chat detail / sources.
    # =====================================================================
    conv_detail_blob = json.dumps(userA.get(f"/chat/conversations/{conv_a}").json())
    assert not _contains_any(conv_detail_blob, b_email, BETA_NAME), "LEAK p7: foreign metadata in conv detail"
    assert "password_hash" not in conv_detail_blob

    # =====================================================================
    # Property 6 — Disabled user.
    # =====================================================================
    # Pre-condition: userB's token works on a protected call right now.
    assert userB.get("/auth/me").status_code == 200
    dis = admin.post(f"/admin/users/{b_me['id']}/disable")
    assert dis.status_code == 200, dis.text
    assert dis.json()["is_active"] is False
    # (a) userB can no longer log in (401).
    relog = anon_client.post("/auth/login", data={"username": b_email, "password": "supersecret123"})
    assert relog.status_code == 401, "LEAK p6: disabled user can still log in"
    # (b) the EXISTING userB token is rejected on a protected call (401).
    assert userB.get("/auth/me").status_code == 401, "LEAK p6: disabled user's existing token still valid"
    assert userB.get("/documents").status_code == 401
    assert userB.post("/chat/query", json={"query": "still here?"}).status_code == 401
    # userA (untouched) still works.
    assert userA.get("/auth/me").status_code == 200, "disabling userB affected userA"
