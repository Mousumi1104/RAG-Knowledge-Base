"""Tests for single-admin JWT auth and the public/protected route split.

Covers ``POST /auth/login`` (form flow), ``GET /auth/me``, the admin-only
``/documents`` management routes (401 without a token, success with one), the
public ``POST /documents/upload`` (anonymous uploads allowed), and confirms the
chat/health public routes stay reachable without credentials. Runs against the
local providers + Postgres test DB wired up in ``conftest.py`` (deterministic,
no network/keys).
"""

from __future__ import annotations

from app.core.config import get_settings
from tests.conftest import upload_file

_SETTINGS = get_settings()


# ---------------------------------------------------------------------------
# 1) Login
# ---------------------------------------------------------------------------
def test_login_correct_creds_returns_bearer_token(anon_client) -> None:
    # Login is by email now: the seeded admin authenticates with ADMIN_EMAIL.
    resp = anon_client.post(
        "/auth/login",
        data={"username": _SETTINGS.ADMIN_EMAIL, "password": _SETTINGS.ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and body["access_token"]


def test_login_wrong_password_401(anon_client) -> None:
    resp = anon_client.post(
        "/auth/login",
        data={"username": _SETTINGS.ADMIN_EMAIL, "password": "definitely-wrong"},
    )
    assert resp.status_code == 401
    assert "detail" in resp.json()


def test_login_unknown_email_401(anon_client) -> None:
    resp = anon_client.post(
        "/auth/login",
        data={"username": "nobody@example.com", "password": "whatever-123"},
    )
    assert resp.status_code == 401


def test_login_missing_fields_422(anon_client) -> None:
    # OAuth2PasswordRequestForm requires both username and password as form data;
    # omitting them is a validation error, not an auth failure.
    resp = anon_client.post("/auth/login", data={"username": _SETTINGS.ADMIN_EMAIL})
    assert resp.status_code == 422


def test_login_json_body_422(anon_client) -> None:
    # Form-encoded only: a JSON body is a validation error (not a 401).
    resp = anon_client.post(
        "/auth/login",
        json={"username": _SETTINGS.ADMIN_EMAIL, "password": _SETTINGS.ADMIN_PASSWORD},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 2) Protected routes require a token
# ---------------------------------------------------------------------------
def test_documents_list_without_token_401(anon_client) -> None:
    resp = anon_client.get("/documents")
    assert resp.status_code == 401


def test_documents_upload_without_token_401(anon_client) -> None:
    # Upload now requires authentication (Milestone 3): anonymous uploads are gone.
    # The document is owned by the authenticated user (owner_id), so an unauth
    # caller is rejected with 401 rather than minting an "anonymous" doc.
    resp = upload_file(anon_client, "nope.txt", "anonymous uploads are not allowed")
    assert resp.status_code == 401


def test_documents_list_with_token_200(client) -> None:
    resp = client.get("/documents")
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_documents_upload_with_token_201(client) -> None:
    resp = upload_file(client, "authed.txt", "Uploaded by an authenticated admin.")
    assert resp.status_code == 201
    assert resp.json()["id"] > 0


# ---------------------------------------------------------------------------
# 3) Public vs. authenticated routes
# ---------------------------------------------------------------------------
def test_health_public(anon_client) -> None:
    resp = anon_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_chat_query_requires_auth(anon_client) -> None:
    # Chat is tenant-scoped now: unauthenticated -> 401; authenticated -> 200.
    assert (
        anon_client.post("/chat/query", json={"query": "What is the leave policy?"}).status_code
        == 401
    )


def test_chat_query_authenticated(client) -> None:
    resp = client.post("/chat/query", json={"query": "What is the leave policy?"})
    assert resp.status_code == 200


def test_search_requires_auth(anon_client) -> None:
    assert anon_client.post("/search", json={"query": "leave policy"}).status_code == 401


def test_search_authenticated(client) -> None:
    resp = client.post("/search", json={"query": "leave policy"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 4) /auth/me
# ---------------------------------------------------------------------------
def test_me_with_token_returns_admin(client) -> None:
    resp = client.get("/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    # /auth/me returns a safe UserOut: id, email, role only (NEVER password_hash).
    assert body["email"] == _SETTINGS.ADMIN_EMAIL
    assert body["role"] == "admin"
    assert isinstance(body["id"], int) and body["id"] > 0
    assert "password_hash" not in body
    assert "username" not in body


def test_me_without_token_401(anon_client) -> None:
    resp = anon_client.get("/auth/me")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 5) Registration + admin user management
# ---------------------------------------------------------------------------
def test_register_creates_user_and_can_login(anon_client) -> None:
    email = "newuser@example.com"
    password = "supersecret123"
    resp = anon_client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == email
    assert body["role"] == "user"
    assert "password_hash" not in body

    # The new user can authenticate.
    login = anon_client.post("/auth/login", data={"username": email, "password": password})
    assert login.status_code == 200


def test_register_duplicate_email_409(anon_client) -> None:
    payload = {"email": "dup@example.com", "password": "supersecret123"}
    first = anon_client.post("/auth/register", json=payload)
    assert first.status_code == 201
    second = anon_client.post("/auth/register", json=payload)
    assert second.status_code == 409


def test_register_short_password_422(anon_client) -> None:
    resp = anon_client.post(
        "/auth/register", json={"email": "shortpw@example.com", "password": "short"}
    )
    assert resp.status_code == 422


def test_admin_can_list_users_no_hashes(client) -> None:
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    users = resp.json()
    assert isinstance(users, list) and len(users) >= 1
    for u in users:
        assert "password_hash" not in u
        assert {"id", "email", "role"} <= set(u.keys())


def test_admin_users_requires_admin(anon_client) -> None:
    resp = anon_client.get("/admin/users")
    assert resp.status_code == 401


def test_admin_disable_enable_and_delete_flow(client, anon_client) -> None:
    # Create a fresh user via registration, then admin-toggle it.
    email = "toggle@example.com"
    password = "supersecret123"
    reg = anon_client.post("/auth/register", json={"email": email, "password": password})
    assert reg.status_code == 201
    uid = reg.json()["id"]

    # Disable -> the user can no longer log in (inactive).
    dis = client.post(f"/admin/users/{uid}/disable")
    assert dis.status_code == 200
    blocked = anon_client.post("/auth/login", data={"username": email, "password": password})
    assert blocked.status_code == 401

    # Enable -> login works again.
    en = client.post(f"/admin/users/{uid}/enable")
    assert en.status_code == 200
    ok = anon_client.post("/auth/login", data={"username": email, "password": password})
    assert ok.status_code == 200

    # Delete -> login fails (soft-deleted).
    dele = client.delete(f"/admin/users/{uid}")
    assert dele.status_code == 200
    gone = anon_client.post("/auth/login", data={"username": email, "password": password})
    assert gone.status_code == 401


def test_admin_cannot_disable_self(client) -> None:
    me = client.get("/auth/me").json()
    resp = client.post(f"/admin/users/{me['id']}/disable")
    assert resp.status_code == 400
