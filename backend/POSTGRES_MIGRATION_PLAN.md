# PostgreSQL + pgvector Migration Plan

**Goal:** Move the FastAPI RAG backend from **SQLite + ChromaDB** to **PostgreSQL + pgvector**, removing SQLite and ChromaDB entirely.

**Target environment (validated):**

- App `DATABASE_URL` = `postgresql+psycopg://raguser@127.0.0.1:54329/ragdb`
- Test `DATABASE_URL` = `postgresql+psycopg://raguser@127.0.0.1:54329/ragdb_test`
- Vector store = **pgvector** (PostgreSQL extension). `VECTOR_DB` default → `pgvector`.
- Local no-sudo userspace PostgreSQL on `127.0.0.1:54329`, role `raguser` (trust auth), databases `ragdb` + `ragdb_test`, both with the `vector` extension, managed by `backend/scripts/pg_local.sh` (authored later).
- Embeddings: keep the existing provider abstraction (local hashing default, `EMBEDDING_DIM=384`). The pgvector column dimension **must equal** `EMBEDDING_DIM`.

> This document is a plan only. **No code is changed by this file.**

---

## 0. Inventory of what exists today

| Concern | Where | Current behavior |
| --- | --- | --- |
| Relational DB default | `app/core/config.py:27` | `DATABASE_URL = "sqlite:///./rag.db"` |
| Vector DB default | `app/core/config.py:30-31` | `VECTOR_DB = "chromadb"`, `CHROMA_DB_PATH = "./chroma_db"` |
| Engine build | `app/database/session.py:23-30` | SQLite branch sets `check_same_thread=False` |
| Common-field mixin | `app/database/base.py:43-66` | `BigInteger().with_variant(Integer, "sqlite")` PK; `DateTime(timezone=True)`; `status` SMALLINT |
| Vector store interface | `app/vectorstore/base.py` | `add / query / delete_by_document / count / health`, `QueryResult` TypedDict |
| Vector store factory | `app/vectorstore/__init__.py:14-25` | returns `ChromaVectorStore`, defaults/falls back to chroma |
| Chroma impl | `app/vectorstore/chroma_store.py` | persistent client, cosine space, `score = 1.0 - distance` |
| Document model | `app/models/document.py` | metadata + indexing lifecycle; no embeddings |
| Chunk model | `app/models/chunk.py` | text + `chunk_id` pointer; **no embedding column** |
| Indexing pipeline | `app/services/indexing_service.py` | embeds chunks, `store.add(...)`, persists `Chunk` rows |
| Search | `app/services/search_service.py` | `store.query(...)`, re-validates active doc/chunk, returns score |
| Admin health/stats | `app/services/admin_service.py:21-38,77,178-194` | calls `get_vector_store().health()/count()` |
| Requirements | `requirements.txt:8` | `chromadb` |
| Env example | `.env.example:10-16` | SQLite + chroma values |
| Alembic env | `migrations/env.py` | reads `settings.DATABASE_URL`; no versions yet |
| Alembic ini | `alembic.ini:8` | `sqlalchemy.url = sqlite:///./rag.db` (placeholder) |
| Tests | `tests/conftest.py` | temp SQLite file + temp Chroma dir; drop/create_all per test |

Embedding contract (`app/rag/embeddings.py`): all providers emit `EMBEDDING_DIM`-length L2-normalized `float32` vectors; local hashing provider is the deterministic default.

---

## 1. SQLite touchpoints (every file + exact change)

1. **`app/core/config.py:27`**
   - `DATABASE_URL: str = "sqlite:///./rag.db"` →
     `DATABASE_URL: str = "postgresql+psycopg://raguser@127.0.0.1:54329/ragdb"`

2. **`app/database/session.py:23-30`** (`_build_engine`)
   - Delete the entire `if url.startswith("sqlite"): connect_args["check_same_thread"] = False` branch.
   - Build a plain psycopg-v3 engine with `pool_pre_ping=True` (see §4).
   - Update the module docstring (lines 1-5) to drop the "runs on SQLite" claim.

3. **`app/database/base.py:39-47`**
   - Replace `BigInteger().with_variant(Integer, "sqlite")` with plain `BigInteger`.
   - Remove `Integer` from the import on line 13 if it becomes unused (it is, in `base.py`).
   - Update the comment block (lines 39-42) to state BIGSERIAL on PostgreSQL only.

4. **`app/database/base.py:1-6, 34-37`** — docstrings: drop "identically on SQLite and PostgreSQL"; `last_modified` `onupdate=func.now()` stays (still valid and needed; a trigger is optional, see §6).

5. **`app/models/document.py:4-6`, `app/models/chunk.py:4-6`** — docstring text only: remove "ChromaDB" / "SQLite and PostgreSQL"; chunk model gains an embedding column (see §3).

6. **`alembic.ini:8`**
   - `sqlalchemy.url = sqlite:///./rag.db` → a Postgres placeholder, e.g.
     `sqlalchemy.url = postgresql+psycopg://raguser@127.0.0.1:54329/ragdb`
     (still overridden at runtime by `env.py`, but should not say sqlite).

7. **`.env.example:9-12`** — replace the SQLite block:
   ```
   # --- Database --------------------------------------------------------------
   DATABASE_URL=postgresql+psycopg://raguser@127.0.0.1:54329/ragdb
   ```

8. **`tests/conftest.py:8-13, 28-37, 79, 87-89`** — remove all SQLite usage (see §7).

9. **Docs (non-blocking but required by "remove SQLite entirely"):**
   `backend/ARCHITECTURE.md`, `backend/VALIDATION_REPORT.md`, and any `*.md` mentioning sqlite — grep and scrub. (No code impact.)

**Verification:** `grep -rin "sqlite\|check_same_thread\|with_variant" app tests migrations alembic.ini .env.example` must return **zero** non-doc hits (ideally zero hits at all).

---

## 2. ChromaDB touchpoints (every file + what to delete/replace)

1. **`app/vectorstore/chroma_store.py`** — **DELETE the file entirely.** It is the only module importing `chromadb` (lines 27-28).

2. **`app/vectorstore/__init__.py:14-25`** — rewrite `get_vector_store()`:
   - Default backend `pgvector`; accept `("pgvector", "pgvec", "postgres")`; unknown → warn and fall back to `pgvector`.
   - `from app.vectorstore.pgvector_store import PgVectorStore; return PgVectorStore()`.
   - Update line 11 `__all__` / docstring wording.

3. **NEW `app/vectorstore/pgvector_store.py`** — implements the same `VectorStore` interface against Postgres (see §3).

4. **`app/vectorstore/base.py:3`** — docstring: change "(e.g. ChromaDB, PGVector later)" to "(pgvector)". Interface is unchanged.

5. **`app/core/config.py:30-31`**
   - `VECTOR_DB: str = "chromadb"` → `VECTOR_DB: str = "pgvector"`
   - **DELETE** `CHROMA_DB_PATH: str = "./chroma_db"`.

6. **`requirements.txt:8`** — remove `chromadb` (see §5).

7. **`.env.example:14-16`** — replace:
   ```
   # --- Vector store ----------------------------------------------------------
   VECTOR_DB=pgvector
   ```
   Delete the `CHROMA_DB_PATH=` line.

8. **`tests/conftest.py`** — remove every Chroma reference (lines 12, 29, 31, 37, 62-66, 80, 84). See §7.

9. **`app/services/indexing_service.py` / `app/services/search_service.py` / `app/services/admin_service.py`** — **no import changes needed**; they use the `get_vector_store()` abstraction. The pgvector store must keep the same method signatures and return shapes so these are untouched (apart from the embedding-column write path, see §3).

10. **`app/.gitignore` / runtime dirs** — drop `chroma_db/` ignore entries; remove any on-disk `chroma_db/` directory.

**Verification:** `grep -rin "chroma" app tests requirements.txt .env.example` returns **zero** hits; `python -c "import chromadb"` is irrelevant (package removed).

---

## 3. pgvector schema design

### Decision: store the embedding as a `Vector(EMBEDDING_DIM)` column **on the existing `chunks` table**.

**Justification (chosen over a dedicated `embeddings` table):**
- One vector per chunk — a strict 1:1 relationship; a separate table adds a join with no normalization benefit.
- The current `Chunk` row already holds `content`, `document_id`, `position`, `chunk_id`, and the `status` soft-delete flag. Co-locating the embedding lets a **single SQL query** do ANN ranking **and** active-filtering (`WHERE status = 1 AND document.status = 1`), eliminating the current over-fetch-then-revalidate dance in `search_service.py`.
- Soft-delete (`status = 5`) automatically excludes the vector from search via the `WHERE` clause — no separate vector deletion needed for soft delete (though `delete_by_document` still nulls/removes rows on reindex).

### New column on `Chunk` (`app/models/chunk.py`)

```python
from pgvector.sqlalchemy import Vector
from app.core.config import settings

embedding: Mapped[list[float] | None] = mapped_column(
    Vector(settings.EMBEDDING_DIM),   # MUST equal EMBEDDING_DIM (384)
    nullable=True,                    # nullable so a chunk can exist pre-embed
)
```

Also widen the FK column type: `document_id` is currently `Integer` (`chunk.py:23`) while `documents.id` is `BigInteger`. **Change `document_id` to `BigInteger`** to match the PK type exactly (avoids a type-mismatch FK on Postgres).

### Distance → similarity (must match current behavior)

Chroma used cosine **distance** `d ∈ [0, 2]` and returned `score = 1.0 - d` (`chroma_store.py:92`). pgvector's cosine distance operator `<=>` returns the **same** cosine distance `1 - cos_sim ∈ [0, 2]`. So the conversion stays identical:

```
score = 1.0 - (embedding <=> :query_vec)
```

This preserves the existing API contract and the `test_search.py:28-30` assertion that results are sorted by `score` descending (equivalently, `<=>` ascending). Because embeddings are L2-normalized, cosine distance == (scaled) L2 ordering, but cosine is used to match the prior semantics exactly.

### ANN index — recommend **HNSW** with `vector_cosine_ops`

```sql
CREATE INDEX ix_chunks_embedding_hnsw
  ON chunks USING hnsw (embedding vector_cosine_ops);
```

**Why HNSW over IVFFlat:**
- HNSW needs **no training pass** and gives good recall **immediately**, even on a near-empty table — critical because the test suite indexes one tiny document and immediately searches (`test_search.py`, `test_indexing.py`). IVFFlat lists are only useful after enough rows exist and after a `REINDEX`/build with representative data; on a 2-chunk table it degrades to ~sequential and is sensitive to `lists`.
- The dataset here is small-to-moderate; HNSW build cost is acceptable and query latency/recall are superior.
- Requires `pgvector >= 0.5.0` for HNSW. If pinning `pgvector>=0.2.5` only (Python client), the **server extension** must be a recent build that supports HNSW (the userspace Postgres `vector` extension confirmed in this env should be ≥0.5). If the server extension predates HNSW, fall back to:
  ```sql
  CREATE INDEX ix_chunks_embedding_ivf
    ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
  ```
  (and accept that it only helps once rows accumulate — see Risks).

The query must match the index opclass: use `<=>` (cosine) so the planner can use the HNSW/IVFFlat cosine index.

### `PgVectorStore` implementation outline (`app/vectorstore/pgvector_store.py`)

Implements `VectorStore` (same signatures), but operates on the `chunks` table via SQLAlchemy. Because chunk rows are already written by `indexing_service`, there are two viable wirings:

- **Option A (minimal churn, recommended):** keep `store.add(...)` / `store.query(...)` / `delete_by_document` / `count` / `health` exactly as the interface defines. `add()` upserts the `embedding` column onto the matching `chunks` rows keyed by `chunk_id` (the stable key). `query()` runs the `<=>` ANN SQL joined to active documents and returns `QueryResult` dicts with `id`, `score`, `metadata` (`document_id`, `chunk_id`, `position`, `content`), `document`. This keeps `indexing_service.py` and `search_service.py` byte-for-byte unchanged except they now talk to a Postgres-backed store.
  - `add()` needs its own short transaction/session (the store has no `db` handle). Give `PgVectorStore` access to `SessionLocal` and do an `UPDATE chunks SET embedding=:e, last_modified=now() WHERE chunk_id=:cid` (the chunk rows already exist; in `index_document` the `store.add` call currently precedes the `Chunk(...)` inserts — **note ordering**, see below).
  - **Ordering caveat in `indexing_service.index_document`:** today `store.add(...)` (line 136) runs **before** the `Chunk(...)` inserts (lines 143-152). With embeddings living on the chunk row, the rows must exist first. Two clean fixes: (a) reorder so chunks are flushed before `store.add`, or (b) have `store.add` perform an upsert keyed by `chunk_id`. **Recommend (a):** insert+flush `Chunk` rows first, then call `store.add` to populate `embedding`. This is the only service-layer code change required.
  - `count()` → `SELECT count(*) FROM chunks WHERE embedding IS NOT NULL AND status = 1`.
  - `delete_by_document(doc_id)` → on reindex, `UPDATE chunks SET embedding = NULL WHERE document_id = :id` (or rely on `_clear_document_chunks` hard-delete which already removes the rows). For soft-delete path, no vector action needed since search filters on `status`.
  - `health()` → `{"backend": "pgvector", "extension": "vector", "table": "chunks", "count": <count>, "status": "ok"}`; must not raise (mirror `chroma_store.health`). `test_admin.py:14` only asserts `status == "ok"`, so the backend label change is safe.

- **Option B (cleaner long-term):** collapse the vector store into `search_service` doing the `<=>` SQL directly and drop the `VectorStore` abstraction. **Not recommended now** — larger blast radius, breaks `admin_service` and `test_indexing.py:56` (`search_service._get_vector_store()`). Keep the abstraction.

---

## 4. Config / engine / base edits (exact)

### `app/core/config.py`
```python
# --- Database ---------------------------------------------------------
DATABASE_URL: str = "postgresql+psycopg://raguser@127.0.0.1:54329/ragdb"

# --- Vector store -----------------------------------------------------
VECTOR_DB: str = "pgvector"
# (CHROMA_DB_PATH removed)
```

### `app/database/session.py` (`_build_engine`)
```python
from sqlalchemy import create_engine

def _build_engine() -> Engine:
    """Create the SQLAlchemy engine for PostgreSQL (psycopg v3)."""
    return create_engine(
        settings.DATABASE_URL,   # postgresql+psycopg://...
        pool_pre_ping=True,      # recycle dead connections (userspace PG restarts)
        future=True,
    )
```
- Remove `connect_args` / the sqlite branch entirely. `check_same_thread` is sqlite-only and must be gone.
- Keep `SessionLocal`, `get_db`, `init_db` as-is. `init_db()`'s `create_all` still works on Postgres **but will NOT create the `vector` extension or the ANN index** — those come from the Alembic migration (§6). For app startup against an already-migrated DB, `create_all` is a harmless no-op; production should run `alembic upgrade head` instead of relying on `create_all`.

### `app/database/base.py`
```python
from sqlalchemy import BigInteger, DateTime, SmallInteger, func   # drop Integer

id: Mapped[int] = mapped_column(
    BigInteger,            # BIGSERIAL on PostgreSQL
    primary_key=True,
    autoincrement=True,
)
```
- `status` SMALLINT default 1 — unchanged.
- `added_time` / `last_modified` `DateTime(timezone=True)` → maps to `timestamptz` on Postgres — unchanged. `server_default=func.now()` → `now()`; `onupdate=func.now()` keeps last_modified auto-updating at the ORM layer. Optionally add a DB trigger in the migration for last_modified, but the ORM `onupdate` already satisfies the contract for app writes.

**Common DB field contract preserved everywhere:** `id` BIGINT PK / BIGSERIAL; `status` SMALLINT default 1 (0=Inactive, 1=Active, 5=Deleted); `added_time` + `last_modified` timestamptz default `now()`, `last_modified` auto-updates; soft-delete sets `status=5`; active-only default reads (`status == Status.ACTIVE.value`) remain in services.

---

## 5. requirements.txt

```
fastapi
uvicorn[standard]
pydantic
pydantic-settings
sqlalchemy>=2.0
alembic
python-multipart
psycopg[binary]>=3.1      # NEW — psycopg v3 driver for postgresql+psycopg://
pgvector>=0.2.5           # NEW — Vector type + SQLAlchemy integration
pypdf
python-docx
httpx
numpy                     # ensure present (embeddings.py imports numpy)
pytest
pytest-asyncio
```
- **Remove** `chromadb` (line 8).
- `psycopg[binary]` provides the v3 DBAPI; do **not** add `psycopg2`. URL scheme `postgresql+psycopg` selects psycopg v3.
- `numpy` is imported by `app/rag/embeddings.py` — add explicitly if it was only a transitive dep of chromadb (chromadb pulled numpy; removing it could break embeddings). **Verify** and pin.

---

## 6. Alembic migration

### `migrations/env.py` changes
- Add `import pgvector.sqlalchemy  # noqa: F401` near the top so the `Vector` type is registered for autogenerate/`compare_type`.
- It already reads `settings.DATABASE_URL` (lines 16, 26, 36, 50) — keep. Now resolves to the Postgres URL automatically.
- Keep `compare_type=True`. Optionally add `process_revision_directives` later; not required.
- No sqlite-specific logic exists here to remove.

### Initial revision `versions/0001_init.py` — `upgrade()` must:
1. `op.execute("CREATE EXTENSION IF NOT EXISTS vector")` — **first**, before creating the embedding column.
2. `create_table("documents", ...)` with the common fields:
   - `id` BIGINT identity (BIGSERIAL) PK
   - `status` SMALLINT NOT NULL server_default `'1'`
   - `added_time` TIMESTAMPTZ NOT NULL server_default `now()`
   - `last_modified` TIMESTAMPTZ NOT NULL server_default `now()`
   - plus `filename`, `document_type`, `uploaded_by` (default 'admin'), `file_path`, `size` BIGINT, `indexing_status` (default 'pending'), `chunk_count` (default 0), `error`.
3. `create_table("chunks", ...)` with common fields plus:
   - `document_id` BIGINT NOT NULL `ForeignKey("documents.id")`, indexed
   - `chunk_id` VARCHAR(128) NOT NULL UNIQUE, indexed
   - `content` TEXT NOT NULL
   - `position` INTEGER NOT NULL default 0
   - `embedding` `Vector(384)` (= `EMBEDDING_DIM`) NULLABLE — import `from pgvector.sqlalchemy import Vector`.
4. Indexes: `ix_chunks_document_id`, unique `ix_chunks_chunk_id`, and the ANN index:
   ```python
   op.execute(
       "CREATE INDEX ix_chunks_embedding_hnsw "
       "ON chunks USING hnsw (embedding vector_cosine_ops)"
   )
   ```
   (IVFFlat fallback documented in §3.)
5. (Optional) a `BEFORE UPDATE` trigger to set `last_modified = now()` at the DB level. Not required because the ORM sets it via `onupdate`; include only if non-ORM writers are expected.

### `downgrade()`:
- Drop the ANN index, drop `chunks`, drop `documents`. Do **not** `DROP EXTENSION vector` (other objects/DBs may depend on it; leave it).

### `alembic.ini`
- Update `sqlalchemy.url` placeholder off sqlite (see §1.6).

### Migration hardening note
- The `Vector(384)` literal must equal `settings.EMBEDDING_DIM`. Either hardcode `384` in the migration (migrations should be immutable/explicit) **and** assert `EMBEDDING_DIM == 384` at app start, or read `settings.EMBEDDING_DIM` in the migration. **Recommend hardcoding 384 in the migration** (migrations are point-in-time) and adding a startup check `assert settings.EMBEDDING_DIM == <vector_col_dim>` documented as a Risk (§9).

---

## 7. Test-suite port (`tests/conftest.py`)

Rewrite the top-of-file setup and the isolation fixture:

1. **Env block (lines 27-40):** remove `_DB_PATH`, `_CHROMA_ROOT`, `_fresh_chroma_path`, `CHROMA_DB_PATH`. Set:
   ```python
   os.environ["EMBEDDING_PROVIDER"] = "local"
   os.environ["LLM_PROVIDER"] = "local"
   os.environ["VECTOR_DB"] = "pgvector"
   os.environ["DATABASE_URL"] = (
       "postgresql+psycopg://raguser@127.0.0.1:54329/ragdb_test"
   )
   os.environ.pop("OPENAI_API_KEY", None)
   os.environ.pop("ANTHROPIC_API_KEY", None)
   ```
   Keep `_UPLOADS_ROOT` (temp uploads dir) and `document_service.UPLOADS_DIR` redirect (lines 30, 59).

2. **Settings cache clear (lines 45-49):** unchanged — still required so the Postgres URL is picked up before `app.database.session` builds the engine.

3. **Schema setup:** the `vector` extension and ANN index are **not** created by `Base.metadata.create_all`. Two strategies:
   - **(Recommended) Alembic-based, once per session:** a session-scoped fixture runs `alembic upgrade head` against `ragdb_test` (creates extension + tables + index). Then per-test isolation **truncates** instead of drop/create:
     ```python
     @pytest.fixture(autouse=True)
     def _isolated_environment():
         _reset_service_caches()
         with db_session.engine.begin() as conn:
             conn.execute(text("TRUNCATE chunks, documents RESTART IDENTITY CASCADE"))
         yield
         _reset_service_caches()
     ```
     `TRUNCATE ... RESTART IDENTITY CASCADE` is fast and resets BIGSERIAL between tests. The HNSW index persists (correct).
   - **(Simpler) create_all + manual extension:** session fixture does `CREATE EXTENSION IF NOT EXISTS vector`, then `Base.metadata.create_all`, then `op.execute` the ANN index manually; per-test `TRUNCATE`. Avoids an Alembic dependency in tests but duplicates DDL. **Prefer Alembic** so tests exercise the real migration.

4. **Remove** `_fresh_chroma_path()` (62-66) and the `monkeypatch.setattr(settings, "CHROMA_DB_PATH", ...)` (line 84) entirely. Per-test vector isolation now comes from `TRUNCATE` of `chunks` (embeddings live there).

5. **`db` / `client` fixtures (96-119):** unchanged (still bind to `db_session.SessionLocal`). `init_db()` run inside `TestClient` lifespan becomes a no-op against the already-migrated DB; harmless.

6. **Per-test isolation guarantee:** `TRUNCATE chunks, documents` clears both relational rows and vectors in one shot (vectors are on `chunks`), giving the same clean-slate guarantee the old drop/create_all + fresh-chroma-dir gave.

7. **Test assertions that stay valid:**
   - `test_indexing.py:56-57,71` (`search_service._get_vector_store().count()`) — `PgVectorStore.count()` returns embedded-chunk count; still `>0` after index, `0` after delete (soft-delete sets `status=5`, count filters on `status=1` and/or `embedding IS NOT NULL`). Ensure `delete_by_document` / soft-delete path makes `count()` reach 0 for the deleted doc.
   - `test_search.py:28-30` (score-descending) — preserved by `score = 1 - (embedding <=> q)` ordering.
   - `test_admin.py:14` (`vector_db.status == "ok"`) — `PgVectorStore.health()` returns `status: "ok"`.

8. **Pre-flight:** tests must skip/fail clearly if `127.0.0.1:54329` is unreachable. Add a session fixture that connects once and `pytest.exit`s with a helpful message ("run backend/scripts/pg_local.sh start") if Postgres/`ragdb_test` is down.

---

## 8. Live e2e smoke

A script (authored later, e.g. `backend/scripts/smoke_e2e.sh`) will:
1. Ensure userspace Postgres is up (`pg_local.sh start`), `ragdb` migrated (`alembic upgrade head`).
2. Boot `uvicorn app.main:app` against `DATABASE_URL=...ragdb` with `VECTOR_DB=pgvector`, local providers, no API keys.
3. Run the full path with assertions:
   - **upload** a known doc (the leave-policy text) → `200`, returns document id.
   - **index** (auto on upload or explicit reindex) → `indexing_status == "indexed"`, `chunk_count > 0`.
   - **search** `"leave approval HR portal"` → returns the doc, `score` descending, scores in `[~ -1, 1]`.
   - **chat** → answer contains load-bearing keywords ("HR portal", "approval").
   - **admin/health** → `database == "ok"` and `vector_db.status == "ok"`.
4. Tear down uvicorn; optionally `TRUNCATE` or drop `ragdb` smoke data.

This validates the real Postgres+pgvector path end-to-end, beyond the unit tests.

---

## 9. Risks & mitigations

1. **Embedding dim must equal the Vector column.** `EMBEDDING_DIM=384` must equal `Vector(384)`. A mismatch causes `pgvector` insert errors (`expected N dimensions`). **Mitigate:** hardcode 384 in the migration; add a startup assertion `settings.EMBEDDING_DIM == 384`; document that changing `EMBEDDING_DIM` requires a new migration (`ALTER ... TYPE vector(M)` + reindex).

2. **IVFFlat needs rows before it helps.** If the env's `vector` extension lacks HNSW, the IVFFlat fallback gives poor recall on the tiny test corpus and requires a build with data + tuned `lists`. **Mitigate:** prefer HNSW (no training, works empty); confirm the userspace extension version supports it.

3. **psycopg v3 URL form.** Must use `postgresql+psycopg://` (not `postgresql://` which picks psycopg2, nor `postgresql+psycopg2`). Install `psycopg[binary]`, not `psycopg2`. **Mitigate:** pin in requirements; the config default already uses `+psycopg`.

4. **BIGSERIAL autoincrement.** Plain `BigInteger` + `autoincrement=True` yields `BIGSERIAL`/identity on Postgres (worked around with `Integer` for sqlite before). Per-test `TRUNCATE ... RESTART IDENTITY` keeps ids deterministic; without `RESTART IDENTITY`, ids grow across tests (assertions must not hardcode ids — current tests don't).

5. **timestamptz / `now()`.** `DateTime(timezone=True)` → `timestamptz`. `last_modified` auto-update relies on ORM `onupdate`; raw SQL writers (e.g. the `UPDATE chunks SET embedding=...` in `PgVectorStore.add`) must set `last_modified = now()` explicitly or add a DB trigger, else last_modified won't refresh on embedding writes.

6. **`store.add` ordering vs chunk rows.** Embeddings now live on `chunks`, so `index_document` must flush `Chunk` rows **before** populating embeddings (today `store.add` runs first). Reorder (recommended) or make `add` an upsert keyed by `chunk_id`.

7. **`create_all` won't create extension/ANN index.** App startup `init_db()`/`create_all` cannot create `CREATE EXTENSION vector` or the HNSW index. **Mitigate:** rely on Alembic (`alembic upgrade head`) for real schema; treat `create_all` as a dev convenience only, and ensure the test harness runs the migration.

8. **numpy dependency.** `embeddings.py` imports numpy, previously satisfied transitively by chromadb. Removing chromadb may drop numpy. **Mitigate:** add `numpy` to requirements explicitly.

9. **Connection liveness.** Userspace `pg_ctl` Postgres may be restarted between runs; stale pooled connections break. **Mitigate:** `pool_pre_ping=True` on the engine (added in §4).

10. **Single role, trust auth, no password.** URL has no password (`raguser@host`), relying on `trust` auth — fine locally, but must never ship to prod. Keep this confined to the local dev/test env.

---

## 10. Change checklist (file-by-file)

| File | Action |
| --- | --- |
| `app/core/config.py` | DATABASE_URL→postgres; VECTOR_DB→pgvector; **delete** CHROMA_DB_PATH |
| `app/database/session.py` | drop sqlite branch; psycopg engine + `pool_pre_ping` |
| `app/database/base.py` | `BigInteger` PK (drop `with_variant`/`Integer`); docstring scrub |
| `app/models/chunk.py` | add `embedding Vector(EMBEDDING_DIM)`; `document_id`→BigInteger; docstring scrub |
| `app/models/document.py` | docstring scrub only |
| `app/vectorstore/chroma_store.py` | **DELETE** |
| `app/vectorstore/pgvector_store.py` | **NEW** — `PgVectorStore(VectorStore)` over `chunks` |
| `app/vectorstore/__init__.py` | factory → pgvector default/fallback |
| `app/vectorstore/base.py` | docstring scrub (interface unchanged) |
| `app/services/indexing_service.py` | reorder: flush chunks before embedding write |
| `app/services/search_service.py` | no change (abstraction preserved) |
| `app/services/admin_service.py` | no change (abstraction preserved) |
| `requirements.txt` | remove chromadb; add psycopg[binary]>=3.1, pgvector>=0.2.5, numpy |
| `.env.example` | postgres URL; VECTOR_DB=pgvector; remove CHROMA_DB_PATH |
| `alembic.ini` | placeholder url off sqlite |
| `migrations/env.py` | `import pgvector.sqlalchemy` |
| `migrations/versions/0001_init.py` | **NEW** — CREATE EXTENSION + tables + Vector col + HNSW index |
| `tests/conftest.py` | postgres test DB; alembic upgrade + TRUNCATE isolation; remove sqlite/chroma |
| `backend/scripts/pg_local.sh` | (later) manage userspace Postgres |
| `backend/scripts/smoke_e2e.sh` | (later) live e2e |
| `*.md` docs | scrub sqlite/chroma mentions |
