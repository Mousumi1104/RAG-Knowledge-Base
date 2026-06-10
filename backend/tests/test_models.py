"""Tests for ORM models: common fields, Status enum, defaults, soft delete."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.database.base import Status
from app.models.chunk import Chunk
from app.models.document import Document


def test_status_enum_values() -> None:
    assert Status.INACTIVE.value == 0
    assert Status.ACTIVE.value == 1
    assert Status.DELETED.value == 5


@pytest.mark.parametrize("model", [Document, Chunk])
def test_models_have_common_fields(model) -> None:
    for field in ("id", "status", "added_time", "last_modified"):
        assert hasattr(model, field), f"{model.__name__} missing {field}"


def test_document_default_status_active_on_insert(db) -> None:
    doc = Document(
        filename="m.txt",
        document_type="txt",
        file_path="/tmp/m.txt",
        size=10,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    assert doc.id is not None and doc.id > 0
    assert doc.status == Status.ACTIVE.value
    assert isinstance(doc.added_time, datetime)
    assert isinstance(doc.last_modified, datetime)
    # Column defaults applied.
    assert doc.indexing_status == "pending"
    assert doc.chunk_count == 0


def test_chunk_default_status_active_on_insert(db) -> None:
    doc = Document(filename="c.txt", document_type="txt", file_path="/tmp/c.txt", size=5)
    db.add(doc)
    db.commit()
    db.refresh(doc)

    chunk = Chunk(
        document_id=doc.id,
        chunk_id=f"{doc.id}:0",
        content="hello world",
        position=0,
    )
    db.add(chunk)
    db.commit()
    db.refresh(chunk)

    assert chunk.id is not None and chunk.id > 0
    assert chunk.status == Status.ACTIVE.value
    assert isinstance(chunk.added_time, datetime)
    assert isinstance(chunk.last_modified, datetime)


def test_soft_delete_sets_status(db) -> None:
    doc = Document(filename="d.txt", document_type="txt", file_path="/tmp/d.txt", size=5)
    db.add(doc)
    db.commit()
    db.refresh(doc)
    assert doc.status == Status.ACTIVE.value

    doc.status = Status.DELETED.value
    db.add(doc)
    db.commit()
    db.refresh(doc)
    assert doc.status == Status.DELETED.value
