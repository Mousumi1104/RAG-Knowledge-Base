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
    resp = anon_client.post(
        "/auth/login",
        data={"username": _SETTINGS.ADMIN_USERNAME, "password": _SETTINGS.ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and body["access_token"]


def test_login_wrong_password_401(anon_client) -> None:
    resp = anon_client.post(
        "/auth/login",
        data={"username": _SETTINGS.ADMIN_USERNAME, "password": "definitely-wrong"},
    )
    assert resp.status_code == 401
    assert "detail" in resp.json()


def test_login_missing_fields_422(anon_client) -> None:
    # OAuth2PasswordRequestForm requires both username and password as form data;
    # omitting them is a validation error, not an auth failure.
    resp = anon_client.post("/auth/login", data={"username": _SETTINGS.ADMIN_USERNAME})
    assert resp.status_code == 422


def test_login_json_body_422(anon_client) -> None:
    # Form-encoded only: a JSON body is a validation error (not a 401).
    resp = anon_client.post(
        "/auth/login",
        json={"username": _SETTINGS.ADMIN_USERNAME, "password": _SETTINGS.ADMIN_PASSWORD},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 2) Protected routes require a token
# ---------------------------------------------------------------------------
def test_documents_list_without_token_401(anon_client) -> None:
    resp = anon_client.get("/documents")
    assert resp.status_code == 401


def test_documents_upload_without_token_public_201(anon_client) -> None:
    # Upload is intentionally public (public upload router): anonymous callers may
    # upload, and the document is attributed to "anonymous" (uploaded_by is not
    # client-controlled). The /documents listing/management routes stay admin-only.
    resp = upload_file(anon_client, "nope.txt", "anonymous uploads are allowed")
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["uploaded_by"] == "anonymous"


def test_documents_list_with_token_200(client) -> None:
    resp = client.get("/documents")
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_documents_upload_with_token_201(client) -> None:
    resp = upload_file(client, "authed.txt", "Uploaded by an authenticated admin.")
    assert resp.status_code == 201
    assert resp.json()["id"] > 0


# ---------------------------------------------------------------------------
# 3) Public routes stay public (no token)
# ---------------------------------------------------------------------------
def test_health_public(anon_client) -> None:
    resp = anon_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_chat_query_public(anon_client) -> None:
    resp = anon_client.post("/chat/query", json={"query": "What is the leave policy?"})
    assert resp.status_code == 200


def test_search_public(anon_client) -> None:
    resp = anon_client.post("/search", json={"query": "leave policy"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 4) /auth/me
# ---------------------------------------------------------------------------
def test_me_with_token_returns_admin(client) -> None:
    resp = client.get("/auth/me")
    assert resp.status_code == 200
    assert resp.json() == {"username": _SETTINGS.ADMIN_USERNAME}


def test_me_without_token_401(anon_client) -> None:
    resp = anon_client.get("/auth/me")
    assert resp.status_code == 401
