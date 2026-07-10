"""Tests for the secret-gated handbook-sync endpoint.

``POST /internal/sync-global-doc`` is the server-to-server bridge the HR-admin
portal calls to push the HR Handbook PDF into the RAG global knowledge base. It
is gated by ``X-Service-Token`` against ``HANDBOOK_SYNC_TOKEN`` (a secret kept
SEPARATE from ``CRON_SECRET``), and implements "replace old" semantics: any
ACTIVE global already tagged with ``ref`` is soft-deleted before the new upload
is created and background-indexed.

The document_service calls are stubbed so the endpoint never touches real
storage / the vector store; we assert the orchestration (replace -> create ->
index) and the auth gate matrix.

Gate matrix:
- HANDBOOK_SYNC_TOKEN set + correct header -> 200, replaces + creates + indexes
- wrong header                             -> 401
- missing header                           -> 401
- HANDBOOK_SYNC_TOKEN unset (default)      -> 503
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.api import internal

_ENDPOINT = "/internal/sync-global-doc"
_TOKEN = "s3cr3t-handbook-token"
_FILES = {"file": ("HR_Handbook.pdf", b"%PDF-1.4 fake", "application/pdf")}


@dataclass
class _FakeSettings:
    """Minimal stand-in exposing just the attribute the gate reads."""

    HANDBOOK_SYNC_TOKEN: str | None


def _set_token(monkeypatch, value: str | None) -> None:
    """Force the router's view of HANDBOOK_SYNC_TOKEN without touching the cache."""
    monkeypatch.setattr(internal, "get_settings", lambda: _FakeSettings(HANDBOOK_SYNC_TOKEN=value))


@pytest.fixture()
def stub_service(monkeypatch):
    """Stub document_service + the bg indexer; record the orchestration calls."""
    calls = SimpleNamespace(listed=[], deleted=[], created=[], indexed=[])

    # One pre-existing global doc tagged with the ref -> must be replaced.
    existing = SimpleNamespace(id=41)

    def _list(db, ref):
        calls.listed.append(ref)
        return [existing]

    def _soft_delete(db, doc_id, owner_id=None):
        calls.deleted.append((doc_id, owner_id))
        return SimpleNamespace(id=doc_id)

    def _create(db, *, upload_file_bytes, filename, uploaded_by, owner_id, is_global, external_ref):
        calls.created.append(
            dict(
                filename=filename,
                uploaded_by=uploaded_by,
                owner_id=owner_id,
                is_global=is_global,
                external_ref=external_ref,
                nbytes=len(upload_file_bytes),
            )
        )
        return SimpleNamespace(id=99)

    monkeypatch.setattr(internal.document_service, "list_active_globals_by_ref", _list)
    monkeypatch.setattr(internal.document_service, "soft_delete_document", _soft_delete)
    monkeypatch.setattr(internal.document_service, "create_document", _create)

    # Background indexer is lazily imported from app.api.documents inside the
    # route, so patch it on that module (TestClient runs bg tasks in-process).
    from app.api import documents

    monkeypatch.setattr(
        documents, "_run_indexing_bg", lambda doc_id, **k: calls.indexed.append(doc_id)
    )
    return calls


def test_correct_token_replaces_creates_and_indexes(anon_client, monkeypatch, stub_service):
    _set_token(monkeypatch, _TOKEN)

    resp = anon_client.post(_ENDPOINT, headers={"X-Service-Token": _TOKEN}, files=_FILES)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"status": "ok", "ref": "hr_handbook", "id": 99, "replaced": [41]}

    # Orchestration: looked up by ref, soft-deleted the old global, created the
    # new one as a ref-tagged global, and queued indexing of the NEW id.
    assert stub_service.listed == ["hr_handbook"]
    assert stub_service.deleted == [(41, None)]
    created = stub_service.created[0]
    assert created["is_global"] is True
    assert created["owner_id"] is None
    assert created["external_ref"] == "hr_handbook"
    assert created["filename"] == "HR_Handbook.pdf"
    assert created["nbytes"] > 0
    assert stub_service.indexed == [99]


def test_custom_ref_is_honored(anon_client, monkeypatch, stub_service):
    _set_token(monkeypatch, _TOKEN)

    resp = anon_client.post(
        _ENDPOINT,
        headers={"X-Service-Token": _TOKEN},
        files=_FILES,
        data={"ref": "insurance_policy"},
    )

    assert resp.status_code == 200
    assert resp.json()["ref"] == "insurance_policy"
    assert stub_service.listed == ["insurance_policy"]
    assert stub_service.created[0]["external_ref"] == "insurance_policy"


def test_wrong_token_is_unauthorized(anon_client, monkeypatch, stub_service):
    _set_token(monkeypatch, _TOKEN)

    resp = anon_client.post(_ENDPOINT, headers={"X-Service-Token": "nope"}, files=_FILES)

    assert resp.status_code == 401
    assert stub_service.created == []  # nothing created / replaced


def test_missing_token_is_unauthorized(anon_client, monkeypatch, stub_service):
    _set_token(monkeypatch, _TOKEN)

    resp = anon_client.post(_ENDPOINT, files=_FILES)

    assert resp.status_code == 401
    assert stub_service.created == []


def test_unconfigured_token_is_service_unavailable(anon_client, monkeypatch, stub_service):
    _set_token(monkeypatch, None)

    resp = anon_client.post(_ENDPOINT, headers={"X-Service-Token": _TOKEN}, files=_FILES)

    assert resp.status_code == 503
    assert stub_service.created == []
