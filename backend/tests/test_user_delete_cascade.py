"""Admin delete-user cascade: a deleted user's docs + vector chunks are removed.

When an admin deletes a user, that user's OWNED documents must be deleted through
the canonical document-deletion path (``document_service.soft_delete_document``)
so their vector chunks/embeddings are cleaned up — no orphaned vectors. Global /
admin-owned documents and OTHER users' documents must be preserved.

Runs against the local providers + Postgres test DB from ``conftest.py``.
TestClient runs BackgroundTasks synchronously after the response, so uploads
index inline (real chunks are written), letting us assert the chunks are gone.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.database.base import Status
from app.main import app
from app.models.chunk import Chunk
from app.models.conversation import Conversation
from app.models.document import Document
from app.services import user_service

_SETTINGS = get_settings()


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
    assert resp.status_code == 200, resp.text
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


def _active_chunks(db, document_id: int) -> int:
    rows = db.execute(
        select(Chunk).where(
            Chunk.document_id == document_id,
            Chunk.status == Status.ACTIVE.value,
        )
    ).scalars().all()
    return len(rows)


def test_delete_user_cascades_to_docs_chunks_and_conversations(anon_client, db) -> None:
    admin = _client_for(_admin_token(anon_client))

    victim_email = _unique_email("victim")
    keep_email = _unique_email("keeper")
    victim_token = _register_login(anon_client, victim_email)
    keep_token = _register_login(anon_client, keep_email)
    victim = _client_for(victim_token)
    keeper = _client_for(keep_token)

    victim_id = victim.get("/auth/me").json()["id"]
    keeper_id = keeper.get("/auth/me").json()["id"]

    # Victim doc, another user's doc, and a global doc.
    v_doc = _upload(victim, "victim_notes.txt", "Victim private content alpha beta gamma.")
    k_doc = _upload(keeper, "keeper_notes.txt", "Keeper private content delta epsilon zeta.")
    g_doc = _upload_global(admin, "handbook.txt", "Global handbook content for everyone.")

    # Sanity: each doc indexed real chunks inline.
    assert _active_chunks(db, v_doc["id"]) > 0, "victim doc should have chunks before delete"
    assert _active_chunks(db, k_doc["id"]) > 0
    assert _active_chunks(db, g_doc["id"]) > 0

    # Give the victim a conversation so we can assert it is cleaned up too.
    convo = Conversation(user_id=victim_id, title="victim convo", status=Status.ACTIVE.value)
    db.add(convo)
    db.commit()
    db.refresh(convo)
    convo_id = convo.id

    # --- Act: admin deletes the victim user (service-level cascade) -----------
    user_service.delete_user(db, victim_id)
    db.expire_all()

    # --- Assert: victim doc + its chunks are gone -----------------------------
    v_row = db.get(Document, v_doc["id"])
    assert v_row is not None and v_row.status == Status.DELETED.value, (
        "victim document should be soft-deleted"
    )
    assert _active_chunks(db, v_doc["id"]) == 0, (
        "victim document's vector chunks must be removed (no orphaned vectors)"
    )

    # --- Assert: other user's doc + global doc are PRESERVED -------------------
    k_row = db.get(Document, k_doc["id"])
    assert k_row is not None and k_row.status == Status.ACTIVE.value, (
        "another user's document must NOT be deleted"
    )
    assert _active_chunks(db, k_doc["id"]) > 0, "keeper's chunks must remain"

    g_row = db.get(Document, g_doc["id"])
    assert g_row is not None and g_row.status == Status.ACTIVE.value, (
        "global document must be preserved"
    )
    assert _active_chunks(db, g_doc["id"]) > 0, "global chunks must remain"

    # --- Assert: victim conversation soft-deleted; user soft-deleted ----------
    c_row = db.get(Conversation, convo_id)
    assert c_row is not None and c_row.status == Status.DELETED.value, (
        "victim's conversation must be soft-deleted"
    )
    assert user_service.get_user(db, victim_id) is None, "victim user must be soft-deleted"
    assert user_service.get_user(db, keeper_id) is not None, "keeper user must remain"
