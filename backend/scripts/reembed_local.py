"""One-off local re-embedding for the multilingual scorecard.

Re-embeds all ACTIVE local docs with the configured provider (Gemini), so the
pgvector index is internally consistent (every active chunk Gemini-embedded).
Handles the huge global docs by capping each to a subset and DEACTIVATING the
overflow chunks (so no active chunk is left with an incompatible/old vector).

Run from backend/:  ./venv/bin/python scripts/reembed_local.py
"""

from __future__ import annotations

import os
import sys
import time

from sqlalchemy import func, select, update

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.database.base import Status  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models.chunk import Chunk  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.rag.embeddings import get_embedding_provider  # noqa: E402
from app.vectorstore import get_vector_store  # noqa: E402

# Cap the huge global docs so cross-doc contamination tests still have competitors
# without burning the Gemini free-tier quota on thousands of chunks.
GLOBAL_CAP = 300
# Embed/persist in small batches with a pause to pace the free-tier rate limit.
BATCH = 16
PAUSE = 0.4


def reembed_document(db, provider, store, doc: Document, cap: int | None) -> int:
    """Re-embed (and cap) one document's ACTIVE chunks. Returns active count."""
    chunks = list(
        db.execute(
            select(Chunk)
            .where(Chunk.document_id == doc.id, Chunk.status == Status.ACTIVE.value)
            .order_by(Chunk.position.asc())
        ).scalars().all()
    )
    if cap is not None and len(chunks) > cap:
        # Deactivate overflow so the index stays consistent (no active chunk with
        # a stale/incompatible vector). Capped subset keeps the doc a competitor.
        overflow_ids = [c.chunk_id for c in chunks[cap:]]
        db.execute(
            update(Chunk)
            .where(Chunk.chunk_id.in_(overflow_ids))
            .values(status=Status.DELETED.value, embedding=None)
        )
        db.commit()
        chunks = chunks[:cap]
        print(f"  doc {doc.id}: capped to {cap} active chunks (deactivated {len(overflow_ids)})")

    total = 0
    for start in range(0, len(chunks), BATCH):
        batch = chunks[start : start + BATCH]
        contents = [c.content for c in batch]
        ids = [c.chunk_id for c in batch]
        embeddings = provider.embed_texts(contents)
        store.add(ids=ids, embeddings=embeddings, metadatas=[{} for _ in batch], documents=contents)
        total += len(batch)
        print(f"  doc {doc.id}: embedded {total}/{len(chunks)}", flush=True)
        time.sleep(PAUSE)

    doc.chunk_count = total
    doc.indexing_status = "indexed"
    db.add(doc)
    db.commit()
    return total


def main() -> None:
    print(f"EMBEDDING_PROVIDER={settings.EMBEDDING_PROVIDER} LLM_PROVIDER={settings.LLM_PROVIDER}")
    provider = get_embedding_provider()
    print("provider:", type(provider).__name__, "dim:", provider.dim)
    store = get_vector_store()

    db = SessionLocal()
    try:
        actives = list(
            db.execute(
                select(Document).where(Document.status == Status.ACTIVE.value)
            ).scalars().all()
        )
        # Cap only the huge global docs (linear-guest, Online_Statistics).
        caps = {41: GLOBAL_CAP, 45: GLOBAL_CAP}
        for doc in actives:
            cap = caps.get(doc.id)
            n = db.execute(
                select(func.count()).select_from(Chunk).where(
                    Chunk.document_id == doc.id, Chunk.status == Status.ACTIVE.value
                )
            ).scalar()
            print(f"Re-embedding doc {doc.id} {doc.filename} ({n} active chunks, cap={cap})")
            reembed_document(db, provider, store, doc, cap)
        print("Done. Active embedded chunks:", store.count())
    finally:
        db.close()


if __name__ == "__main__":
    main()
