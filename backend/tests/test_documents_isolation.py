"""Milestone 3 — server-side document ownership + tenant isolation tests.

Asserts (all enforced server-side, never trusting the client):
- Upload requires auth and stamps ``owner_id`` (is_global False).
- A user's library lists ONLY their own docs (not other users', not globals).
- User A cannot get/delete/reindex/file-serve User B's doc (404, no IDOR leak).
- Admin global upload sets ``is_global`` True / ``owner_id`` None and is admin-only.
- File-serve requires auth and enforces own-or-global; foreign → 404.
- Admin (sees-all) can act on any user's document.

Runs against the local providers + Postgres test DB from ``conftest.py``. Each
helper uses a unique email so users (which persist across the per-test data
truncation of chunks/documents) never collide.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

_SETTINGS = get_settings()


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def _register_and_token(client: TestClient, email: str, password: str = "supersecret123") -> str:
    """Register a fresh user and return their bearer token."""
    reg = client.post("/auth/register", json={"email": email, "password": password})
    assert reg.status_code == 201, reg.text
    login = client.post("/auth/login", data={"username": email, "password": password})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _user_client(token: str) -> TestClient:
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def _upload(client: TestClient, name: str, content: str):
    return client.post(
        "/documents/upload",
        files={"file": (name, content.encode("utf-8"), "text/plain")},
    )


def _admin_token(client: TestClient) -> str:
    resp = client.post(
        "/auth/login",
        data={"username": _SETTINGS.ADMIN_EMAIL, "password": _SETTINGS.ADMIN_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Upload ownership
# ---------------------------------------------------------------------------
def test_user_upload_sets_owner_and_not_global(anon_client) -> None:
    token = _register_and_token(anon_client, _unique_email("owner"))
    user = _user_client(token)
    me = user.get("/auth/me").json()

    resp = _upload(user, "mine.txt", "A document owned by me.")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["owner_id"] == me["id"]
    assert body["is_global"] is False
    assert body["uploaded_by"] == me["email"]


def test_upload_requires_auth(anon_client) -> None:
    resp = _upload(anon_client, "anon.txt", "no token, no upload")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# List isolation
# ---------------------------------------------------------------------------
def test_user_lists_only_own_documents(anon_client) -> None:
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    b_token = _register_and_token(anon_client, _unique_email("bob"))
    alice, bob = _user_client(a_token), _user_client(b_token)

    a_doc = _upload(alice, "alice.txt", "Alice secret content.").json()
    b_doc = _upload(bob, "bob.txt", "Bob secret content.").json()

    a_ids = {d["id"] for d in alice.get("/documents").json()["items"]}
    b_ids = {d["id"] for d in bob.get("/documents").json()["items"]}

    assert a_doc["id"] in a_ids and b_doc["id"] not in a_ids
    assert b_doc["id"] in b_ids and a_doc["id"] not in b_ids


def test_user_library_excludes_globals(anon_client, client) -> None:
    # Admin uploads a global doc; a user's personal library must not show it.
    g = client.post(
        "/documents/global",
        files={"file": ("global.txt", b"Company-wide policy.", "text/plain")},
    ).json()
    assert g["is_global"] is True

    token = _register_and_token(anon_client, _unique_email("libuser"))
    user = _user_client(token)
    own = _upload(user, "own.txt", "My own doc.").json()

    ids = {d["id"] for d in user.get("/documents").json()["items"]}
    assert own["id"] in ids
    assert g["id"] not in ids  # globals are searchable but not in the library


# ---------------------------------------------------------------------------
# Get / delete / reindex cross-tenant isolation (404, no IDOR)
# ---------------------------------------------------------------------------
def test_user_cannot_get_other_users_doc_404(anon_client) -> None:
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    b_token = _register_and_token(anon_client, _unique_email("bob"))
    alice, bob = _user_client(a_token), _user_client(b_token)

    a_doc = _upload(alice, "alice.txt", "Alice private.").json()
    # Bob asks for Alice's doc by id -> 404 (same as missing; no existence leak).
    assert bob.get(f"/documents/{a_doc['id']}").status_code == 404


def test_user_cannot_delete_other_users_doc_404(anon_client) -> None:
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    b_token = _register_and_token(anon_client, _unique_email("bob"))
    alice, bob = _user_client(a_token), _user_client(b_token)

    a_doc = _upload(alice, "alice.txt", "Alice private.").json()
    assert bob.delete(f"/documents/{a_doc['id']}").status_code == 404
    # Alice's doc must still be intact/active for her.
    assert alice.get(f"/documents/{a_doc['id']}").status_code == 200


def test_user_cannot_reindex_other_users_doc_404(anon_client) -> None:
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    b_token = _register_and_token(anon_client, _unique_email("bob"))
    alice, bob = _user_client(a_token), _user_client(b_token)

    a_doc = _upload(alice, "alice.txt", "Alice private.").json()
    assert bob.post(f"/documents/{a_doc['id']}/reindex").status_code == 404


def test_user_cannot_delete_global_doc_404(anon_client, client) -> None:
    g = client.post(
        "/documents/global",
        files={"file": ("global.txt", b"Company-wide policy.", "text/plain")},
    ).json()
    token = _register_and_token(anon_client, _unique_email("user"))
    user = _user_client(token)
    # Users cannot delete a global doc (delete is own-only, no allow_global) -> 404.
    assert user.delete(f"/documents/{g['id']}").status_code == 404


# ---------------------------------------------------------------------------
# Admin global upload
# ---------------------------------------------------------------------------
def test_admin_global_upload_sets_is_global(client) -> None:
    resp = client.post(
        "/documents/global",
        files={"file": ("g.txt", b"Grounds every answer.", "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_global"] is True
    assert body["owner_id"] is None


def test_global_upload_requires_admin(anon_client) -> None:
    # Unauthenticated -> 401.
    assert (
        anon_client.post(
            "/documents/global",
            files={"file": ("g.txt", b"x", "text/plain")},
        ).status_code
        == 401
    )
    # Authenticated non-admin user -> 403.
    token = _register_and_token(anon_client, _unique_email("nonadmin"))
    user = _user_client(token)
    assert (
        user.post(
            "/documents/global",
            files={"file": ("g.txt", b"x", "text/plain")},
        ).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# File serve: auth + own-or-global enforcement
# ---------------------------------------------------------------------------
def test_file_serve_requires_auth(anon_client) -> None:
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    alice = _user_client(a_token)
    a_doc = _upload(alice, "alice.txt", "Alice private file.").json()
    # No Authorization header -> 401 (was previously public).
    assert anon_client.get(f"/documents/{a_doc['id']}/file").status_code == 401


def test_file_serve_owner_can_access(anon_client) -> None:
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    alice = _user_client(a_token)
    a_doc = _upload(alice, "alice.txt", "Alice private file.").json()
    resp = alice.get(f"/documents/{a_doc['id']}/file")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


def test_file_serve_foreign_doc_404(anon_client) -> None:
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    b_token = _register_and_token(anon_client, _unique_email("bob"))
    alice, bob = _user_client(a_token), _user_client(b_token)
    a_doc = _upload(alice, "alice.txt", "Alice private file.").json()
    # Bob may not stream Alice's private file -> 404 (no existence leak).
    assert bob.get(f"/documents/{a_doc['id']}/file").status_code == 404


def test_file_serve_global_doc_accessible_to_any_user(anon_client, client) -> None:
    g = client.post(
        "/documents/global",
        files={"file": ("g.txt", b"Global readable by all.", "text/plain")},
    ).json()
    token = _register_and_token(anon_client, _unique_email("reader"))
    user = _user_client(token)
    # A user may stream a global doc's file (allow_global=True on /file).
    assert user.get(f"/documents/{g['id']}/file").status_code == 200


# ---------------------------------------------------------------------------
# Admin sees / acts on all
# ---------------------------------------------------------------------------
def test_admin_can_get_and_delete_any_users_doc(anon_client, client) -> None:
    a_token = _register_and_token(anon_client, _unique_email("alice"))
    alice = _user_client(a_token)
    a_doc = _upload(alice, "alice.txt", "Alice doc admin will manage.").json()

    # Admin (sees-all) can fetch and delete another user's doc.
    assert client.get(f"/documents/{a_doc['id']}").status_code == 200
    assert client.delete(f"/documents/{a_doc['id']}").status_code == 200
    # After admin delete it is gone for the owner too.
    assert alice.get(f"/documents/{a_doc['id']}").status_code == 404
