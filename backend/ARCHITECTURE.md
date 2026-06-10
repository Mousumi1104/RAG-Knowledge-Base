# Architecture — AI-Powered RAG Knowledge Base / Chatbot (Backend)

This document defines the architecture for the FastAPI + RAG backend described in
`REQUIREMENTS.md`, `AI_CONTRACT.md`, `DONE_CRITERIA.md`, and `README.md`. It is the
authoritative design reference for implementation.

## Guiding Principles

- **Strict layer separation** between API, document processing, embedding/indexing, vector
  storage, and LLM response generation, as mandated by `REQUIREMENTS.md`.
- **Config-driven** via `pydantic-settings`; no secrets in code (`AI_CONTRACT.md`).
- **No-keys by default**: runs and passes tests with **no external API keys and no
  torch/sentence-transformers** — local deterministic providers are the defaults. The
  relational + vector store is PostgreSQL with pgvector; a self-contained no-sudo
  userspace Postgres 16 + pgvector dev instance is auto-provisioned by
  `scripts/pg_local.sh`, so no system database install is needed for local development.
- **Pluggable** embedding provider and LLM provider behind interfaces so new
  providers/document types can be added without major refactoring.
- **Alembic-managed schema** — the database schema (extension, tables, vector index) is
  created and versioned by Alembic migrations.

## Locked Decisions

| Concern          | Decision                                                                                          |
| ---------------- | ------------------------------------------------------------------------------------------------- |
| Vector store     | **pgvector** — embeddings stored as a `Vector(384)` column on `chunks` with an HNSW cosine index. |
| Embeddings       | Multi-provider (OpenAI, Anthropic-compatible) + **local deterministic hashing/n-gram fallback**.  |
| LLM              | Multi-provider (OpenAI, Anthropic) + **local extractive/templated answerer fallback**.            |
| No heavy deps    | **No torch, no sentence-transformers.** Local providers are pure-Python / dependency-light.        |
| Database         | **PostgreSQL** (psycopg v3) via SQLAlchemy; schema managed by **Alembic** migrations.              |
| Local dev DB     | No-sudo userspace **Postgres 16 + pgvector** via `scripts/pg_local.sh` (127.0.0.1:54329).          |
| Frontend         | Minimal **Next.js** POC.                                                                            |

---

## 1. Layered Architecture

The backend follows a clean, layered structure under `backend/app/`. Each layer depends
only on the layers below it; the API layer never talks to the vector store or providers
directly — it goes through services.

```
HTTP Request
    │
    ▼
┌─────────────┐   request/response validation (Pydantic)
│   api/      │   thin routers: parse, validate, delegate, format
└─────┬───────┘
      ▼
┌─────────────┐   business logic / orchestration
│  services/  │   document lifecycle, search, chat, admin stats
└─────┬───────┘
      ▼
┌──────────────────────────────────────────────────┐
│  rag/      │ vectorstore/ │ database/ │ providers  │
│  chunking, │ pgvector +   │ engine,   │ embeddings │
│  pipeline, │ ABC          │ session,  │ + LLM ABCs │
│  prompting │              │ Base      │            │
└──────────────────────────────────────────────────┘
      ▼
┌─────────────┐
│  models/    │  SQLAlchemy ORM (Document, Chunk) + Base/Mixin
│  schemas/   │  Pydantic DTOs (request/response shapes)
│  utils/     │  logging, errors, file validation, text cleaning
└─────────────┘
```

### Layer responsibilities

- **`api/`** — FastAPI routers only. Validate input with Pydantic schemas, inject
  dependencies (DB session, services), call a service method, and shape the JSON
  response. No business logic. Maps domain/service exceptions to HTTP responses.
- **`services/`** — All business logic and orchestration. Owns the document lifecycle
  (create → extract → chunk → embed → index), the search flow, the RAG chat flow, and
  admin statistics. Coordinates `rag`, `vectorstore`, `database`, and providers.
- **`rag/`** — RAG-specific building blocks independent of transport: text extraction,
  cleaning, chunking (size/overlap), prompt construction, context assembly, and the
  end-to-end indexing pipeline. Provider/vectorstore-agnostic.
- **`vectorstore/`** — The `VectorStore` abstract interface plus the pgvector
  implementation. Handles add/query/delete of embeddings (the `chunks.embedding`
  `Vector(384)` column) and per-chunk metadata, using the HNSW cosine index for ANN
  search.
- **`database/`** — SQLAlchemy engine + session factory, `get_db` dependency, declarative
  `Base`. The engine is built from `DATABASE_URL` (PostgreSQL via psycopg v3); schema is
  applied by Alembic (`alembic upgrade head`).
- **`models/`** — ORM models (`Document`, `Chunk`) and the reusable
  `Base` + `TimestampStatusMixin`.
- **`schemas/`** — Pydantic request/response models (the API contract). Decoupled from
  ORM models.
- **`utils/`** — Cross-cutting helpers: structured logging setup, custom exceptions +
  handlers, file validation, MIME/extension checks, text sanitization.
- **`providers/`** (a sub-package of services/rag, see tree) — Embedding and LLM provider
  abstractions + concrete OpenAI / Anthropic / local implementations, plus a factory that
  selects the provider from config and falls back to local when no key is present.

### Cross-cutting concerns

- **Type hints** everywhere; functions and methods are fully annotated (enforced by
  `AI_CONTRACT.md` code-quality rules).
- **Structured error handling** — a small hierarchy of domain exceptions
  (`NotFoundError`, `ValidationError`, `UnsupportedFileTypeError`, `ProviderError`,
  `RateLimitError`) mapped to consistent JSON error responses via FastAPI exception
  handlers. No bare `except`; provider/IO failures are caught and surfaced meaningfully.
- **Logging** — central logger config in `utils/logging.py`. Each layer logs at
  appropriate levels (request lifecycle, indexing transitions, provider calls, errors with
  stack traces). No secrets are ever logged.

---

## 2. Configuration (pydantic-settings)

A single `app/config.py` exposes a cached `Settings` object loaded from environment
variables / `.env`. **No secret is ever hardcoded**; absence of API keys is a valid,
supported state that activates the local fallback providers.

| Setting              | Type        | Default                                                       | Purpose                                                            |
| -------------------- | ----------- | ------------------------------------------------------------ | ----------------------------------------------------------------- |
| `DATABASE_URL`       | `str`       | `postgresql+psycopg://raguser@127.0.0.1:54329/ragdb`         | SQLAlchemy URL (PostgreSQL via psycopg v3). Defaults to the local `pg_local.sh` instance; point at any Postgres with pgvector for prod. |
| `VECTOR_DB`          | `str`       | `pgvector`                                                   | Selects the `VectorStore` implementation.                          |
| `EMBEDDING_PROVIDER` | `str`       | `local`                                                      | `openai` \| `anthropic` \| `local`.                                |
| `LLM_PROVIDER`       | `str`       | `local`                       | `openai` \| `anthropic` \| `local`.                                |
| `OPENAI_API_KEY`     | `str\|None` | `None`                        | Secret. If unset, OpenAI provider is unavailable → local fallback. |
| `ANTHROPIC_API_KEY`  | `str\|None` | `None`                        | Secret. If unset, Anthropic provider is unavailable → fallback.    |
| `EMBEDDING_DIM`      | `int`       | `384`                         | Vector dimension; local embedder produces exactly this many dims.  |
| `CHUNK_SIZE`         | `int`       | `1000`                        | Max characters/tokens per chunk.                                   |
| `CHUNK_OVERLAP`      | `int`       | `200`                         | Overlap between adjacent chunks (semantic continuity).             |
| `TOP_K`              | `int`       | `5`                           | Default number of chunks retrieved for search/RAG.                 |
| `MAX_UPLOAD_MB`      | `int`       | `25`                          | Max upload size; enforced before persisting.                       |
| `ALLOWED_EXTENSIONS` | `list[str]` | `["pdf","docx","txt","md"]`   | Permitted upload types.                                            |
| `CORS_ORIGINS`       | `list[str]` | `["http://localhost:3000"]`   | Allowed frontend origins.                                          |
| `ADMIN_USERNAME`     | `str`       | `admin`                       | The single admin's login name (no users table).                    |
| `ADMIN_PASSWORD`     | `str`       | `admin123`                    | The single admin's password. **Insecure default — override in prod.** |
| `JWT_SECRET_KEY`     | `str`       | `dev-secret-change-me-in-production` | HS256 signing key. **Insecure default — override in prod.**  |
| `JWT_ALGORITHM`      | `str`       | `HS256`                       | JWT signing algorithm.                                             |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `int` | `720`                       | Token lifetime in minutes (12h).                                  |

> **Production:** `ADMIN_PASSWORD` and `JWT_SECRET_KEY` ship with insecure dev defaults so
> tests/local dev need zero extra env. They **must** be overridden with strong values
> before any non-local deployment (see §10 Auth & Route Protection).

Notes:
- `Settings` is created via `@lru_cache` (`get_settings()`) and injected as a FastAPI
  dependency for testability (overridable in tests).
- Provider selection is **defensive**: even if `EMBEDDING_PROVIDER=openai` but
  `OPENAI_API_KEY` is missing, the factory logs a warning and falls back to `local` so the
  app always boots and tests pass with no keys.

---

## 3. Common DB Field Rules

Per `REQUIREMENTS.md` / `AI_CONTRACT.md`, **every** table includes the same common fields,
provided by a reusable declarative `Base` plus a `TimestampStatusMixin`.

```python
# models/base.py (illustrative)
from datetime import datetime
from sqlalchemy import BigInteger, SmallInteger, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class TimestampStatusMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    added_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_modified: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )
```

| Field           | Type / Rule                                                              |
| --------------- | ----------------------------------------------------------------------- |
| `id`            | Integer/BIGINT primary key, autoincrement.                              |
| `status`        | SMALLINT, NOT NULL, default `1`.                                         |
| `added_time`    | Timestamp, NOT NULL, default `now()`.                                    |
| `last_modified` | Timestamp, NOT NULL, default `now()`, **auto-updates** on modification. |

**Status semantics:** `0 = Inactive`, `1 = Active`, `5 = Deleted`.

**Soft delete (preferred):** `DELETE /documents/{id}` sets `status = 5` and clears the
document's chunk embeddings from the vector store; it does not hard-delete the row.

**Active-by-default reads:** All list/read APIs filter `status = 1` (active) unless an
explicit flag requests otherwise. A shared query helper applies this filter so it can't be
forgotten per-endpoint.

**PostgreSQL types:** `BigInteger` autoincrement, `SmallInteger`, and timezone-aware
`DateTime` map cleanly onto PostgreSQL. `last_modified` uses `server_default=func.now()`
with ORM-level `onupdate=func.now()` so it updates consistently on every modification.

---

## 4. Data Model

PostgreSQL tables. **Embeddings live in the same database via pgvector** — the
`chunks.embedding` `Vector(384)` column — alongside chunk metadata and text. The schema
(the `vector` extension, both tables, and the HNSW cosine index) is created by the Alembic
migration `migrations/versions/0001_initial_pg.py` (`alembic upgrade head`).

### `documents`

| Column            | Type        | Notes                                                              |
| ----------------- | ----------- | ----------------------------------------------------------------- |
| `id`              | BIGINT PK   | Common field.                                                     |
| `status`          | SMALLINT    | Common field (0/1/5).                                             |
| `added_time`      | TIMESTAMP   | Common field.                                                     |
| `last_modified`   | TIMESTAMP   | Common field.                                                     |
| `filename`        | VARCHAR     | Original filename.                                                |
| `document_type`   | VARCHAR     | `pdf` \| `docx` \| `txt` \| `md`.                                  |
| `stored_path`     | VARCHAR     | Path under `uploads/`.                                            |
| `size_bytes`      | BIGINT      | File size.                                                        |
| `content_hash`    | VARCHAR     | Hash for dedup / re-upload detection.                             |
| `uploaded_by`     | VARCHAR     | Uploader identity (e.g. email/admin).                            |
| `indexing_status` | VARCHAR     | `pending` \| `processing` \| `indexed` \| `failed`.               |
| `chunk_count`     | INTEGER     | Number of chunks produced.                                        |
| `error_message`   | TEXT (null) | Last indexing error, if `failed`.                                 |

### `chunks`

| Column          | Type        | Notes                                                              |
| --------------- | ----------- | ----------------------------------------------------------------- |
| `id`            | BIGINT PK   | Common field.                                                     |
| `status`        | SMALLINT    | Common field.                                                     |
| `added_time`    | TIMESTAMP   | Common field.                                                     |
| `last_modified` | TIMESTAMP   | Common field.                                                     |
| `document_id`   | BIGINT FK   | → `documents.id`.                                                 |
| `chunk_index`   | INTEGER     | Position within the document.                                     |
| `vector_id`     | VARCHAR     | Stable per-chunk identifier.                                      |
| `content`       | TEXT        | Chunk text (used for excerpts/source display and local search).   |
| `token_count`   | INTEGER     | Approx token/char count.                                          |
| `embedding`     | `Vector(384)` | pgvector column holding the chunk embedding (`EMBEDDING_DIM` floats); HNSW `vector_cosine_ops` index for ANN search. |

ANN search uses cosine distance over the `embedding` column via the HNSW index; results
are joined back to the `chunks`/`documents` rows for content and source metadata.

Relationship: one `Document` → many `Chunk`. Deleting a document (soft delete) clears its
chunk embeddings and marks chunks `status = 5`.

### `conversations`

A persisted chat thread. Reuses the common `TimestampStatusMixin` fields.

| Column          | Type        | Notes                                                                 |
| --------------- | ----------- | --------------------------------------------------------------------- |
| `id`            | BIGINT PK   | Common field.                                                         |
| `status`        | SMALLINT    | Common field (0/1/5); soft delete sets `5`.                           |
| `added_time`    | TIMESTAMP   | Common field.                                                         |
| `last_modified` | TIMESTAMP   | Common field; ordering key for "newest first" listing.               |
| `title`         | VARCHAR(512) (null) | Auto-derived (truncated) from the first user message.        |

### `messages`

A single chat turn within a conversation. Reuses the common fields.

| Column            | Type        | Notes                                                            |
| ----------------- | ----------- | --------------------------------------------------------------- |
| `id`              | BIGINT PK   | Common field; also the chronological ordering key (id ASC).     |
| `status`          | SMALLINT    | Common field; soft delete sets `5`.                             |
| `added_time`      | TIMESTAMP   | Common field.                                                   |
| `last_modified`   | TIMESTAMP   | Common field.                                                   |
| `conversation_id` | BIGINT FK   | → `conversations.id` (indexed).                                 |
| `role`            | VARCHAR(16) | `user` \| `assistant`.                                          |
| `content`         | TEXT        | The message text.                                              |
| `sources`         | JSONB (null) | Assistant turns: array of `{document, excerpt, document_id, score}`; `NULL` for user turns. |

Relationship: one `Conversation` → many `Message`. List/read paths filter `status = 1`
and order messages by `id ASC`; soft-deleting a conversation also marks its messages
`status = 5`.

---

## 5. Provider Abstraction

Two interfaces, each with OpenAI / Anthropic-compatible / local implementations, chosen by
config via a factory. The local implementations are the default and require **no API keys
and no heavy ML dependencies**.

### `EmbeddingProvider` (interface)

```python
class EmbeddingProvider(Protocol):
    dim: int
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
```

- **`OpenAIEmbeddingProvider`** — calls OpenAI embeddings API (needs `OPENAI_API_KEY`).
- **`AnthropicEmbeddingProvider`** — Anthropic / Anthropic-compatible embeddings endpoint
  (needs `ANTHROPIC_API_KEY`).
- **`LocalEmbeddingProvider`** — deterministic hashing / n-gram (character/word-level)
  embedder producing fixed `EMBEDDING_DIM` vectors, L2-normalized. Pure Python (uses only
  stdlib hashing + lightweight math, optionally numpy). Deterministic → reproducible tests.

### `LLMProvider` (interface)

```python
class LLMProvider(Protocol):
    def generate(self, prompt: str, context: list[ContextChunk]) -> LLMResult: ...
```

- **`OpenAILLMProvider`** / **`AnthropicLLMProvider`** — chat-completion calls to the
  respective APIs with the RAG prompt.
- **`LocalLLMProvider`** — extractive / templated answerer: selects and stitches the most
  relevant retrieved chunks into a grounded, templated answer. No external call; never
  hallucinates beyond retrieved context.

### Selection & fallback

```
EMBEDDING_PROVIDER / LLM_PROVIDER
        │
        ▼
   provider factory
        │
   key present? ──no──► log warning, use Local*
        │ yes
        ▼
   instantiate OpenAI/Anthropic provider
```

Default is `local` for both. Selecting `openai`/`anthropic` without the matching key logs a
warning and **falls back to local** so the system always runs.

---

## 6. Indexing Status Lifecycle

`Document.indexing_status` transitions:

```
        upload                pipeline start
pending ───────────► pending ──────────────► processing
                                                  │
                          extract→clean→chunk→embed→store
                                                  │
                          success ───────────► indexed
                                                  │
                          exception ──────────► failed (error_message set)

reindex (POST /documents/{id}/reindex or /admin/reindex):
indexed | failed ───────────────► processing ──► indexed | failed
```

- **pending** — row created, file stored, not yet processed.
- **processing** — extraction/chunking/embedding/storing in progress.
- **indexed** — all chunks embedded and stored in the vector store.
- **failed** — an error occurred; `error_message` captured; document re-indexable.

Reindex clears old vectors/chunks, then re-runs the pipeline. Admin endpoints surface
per-status counts.

---

## 7. Planned Folder / File Tree

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py                     # FastAPI app, router includes, exception handlers, startup
│   ├── core/
│   │   ├── config.py               # pydantic-settings Settings + get_settings()
│   │   └── security.py             # JWT helpers + get_current_admin dependency
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py                 # get_db, service providers, settings injection
│   │   ├── auth.py                 # /auth/login (public), /auth/me (protected)
│   │   ├── documents.py            # /documents routes (admin JWT)
│   │   ├── search.py               # /search route (public)
│   │   ├── chat.py                 # /chat/* routes (public)
│   │   └── admin.py                # /admin routes (admin JWT)
│   ├── services/
│   │   ├── __init__.py
│   │   ├── document_service.py     # upload, list, get, soft-delete, reindex orchestration
│   │   ├── search_service.py       # embed query → vector search → rank
│   │   ├── chat_service.py         # retrieve → build prompt → LLM → sources
│   │   └── admin_service.py        # stats, indexing-status, health, bulk reindex
│   ├── rag/
│   │   ├── __init__.py
│   │   ├── extractor.py            # PDF/DOCX/TXT/MD text extraction
│   │   ├── cleaner.py              # text cleaning/normalization
│   │   ├── chunker.py              # CHUNK_SIZE/CHUNK_OVERLAP chunking
│   │   ├── prompt.py               # RAG prompt/context assembly
│   │   ├── pipeline.py             # end-to-end indexing pipeline
│   │   └── providers/
│   │       ├── __init__.py
│   │       ├── base.py             # EmbeddingProvider + LLMProvider interfaces
│   │       ├── factory.py          # config-driven selection + local fallback
│   │       ├── embedding_openai.py
│   │       ├── embedding_anthropic.py
│   │       ├── embedding_local.py  # hashing/n-gram embedder (no torch)
│   │       ├── llm_openai.py
│   │       ├── llm_anthropic.py
│   │       └── llm_local.py        # extractive/templated answerer
│   ├── vectorstore/
│   │   ├── __init__.py
│   │   ├── base.py                 # VectorStore interface (add/query/delete)
│   │   ├── pgvector_store.py       # pgvector implementation (Vector(384) + HNSW cosine)
│   │   └── factory.py              # VECTOR_DB-driven selection
│   ├── database/
│   │   ├── __init__.py
│   │   └── session.py              # engine, SessionLocal, get_db
│   ├── models/
│   │   ├── __init__.py
│   │   ├── base.py                 # Base + TimestampStatusMixin
│   │   ├── document.py             # Document model
│   │   └── chunk.py                # Chunk model
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── document.py             # DocumentRead, DocumentList, UploadResponse
│   │   ├── search.py               # SearchRequest, SearchResult
│   │   ├── chat.py                 # ChatRequest, ChatResponse, Source
│   │   └── admin.py                # StatsResponse, IndexingStatusResponse, Health
│   └── utils/
│       ├── __init__.py
│       ├── logging.py              # structured logging config
│       ├── errors.py               # domain exceptions + handlers
│       ├── file_validation.py      # extension/MIME/size checks, safe filename
│       └── text.py                 # sanitization helpers
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # ragdb_test (Postgres+pgvector) + local-provider fixtures
│   ├── test_documents.py
│   ├── test_search.py
│   ├── test_chat.py
│   └── test_admin.py
├── migrations/                     # Alembic migrations
│   └── versions/
│       └── 0001_initial_pg.py      # vector extension, documents/chunks, HNSW index
├── scripts/
│   ├── pg_local.sh                 # no-sudo userspace Postgres 16 + pgvector (up/stop/...)
│   └── e2e_smoke.sh                # live end-to-end smoke check
├── uploads/                        # stored uploaded files (gitignored)
├── alembic.ini
├── .env.example
├── requirements.txt
└── main.py                         # thin entrypoint → app.main:app

frontend/
├── src/
│   ├── app/
│   │   ├── layout.tsx
│   │   ├── page.tsx                # public chat UI (no login)
│   │   └── admin/page.tsx          # login-gated: documents + indexing dashboard
│   ├── components/
│   │   ├── UploadForm.tsx
│   │   ├── ChatBox.tsx
│   │   ├── SearchResults.tsx
│   │   └── SourceList.tsx          # source reference section
│   └── services/
│       └── api.ts                  # backend API client
├── package.json
├── next.config.js
├── tsconfig.json
└── .env.local.example
```

---

## 8. API Surface

All responses are structured JSON with validation and meaningful error responses.
Access column: **Public** = no auth; **Admin** = requires a valid admin JWT
(`Authorization: Bearer <token>`), enforced at the router level (see §11).

### Auth

| Method & Path     | Access | Purpose                                                                       |
| ----------------- | ------ | ----------------------------------------------------------------------------- |
| `POST /auth/login`| Public | OAuth2 password flow. Form fields `username`+`password` → `{access_token, token_type:"bearer"}`. |
| `GET /auth/me`    | Admin  | Returns the authenticated admin identity (`{username}`).                       |

### Documents (all Admin)

| Method & Path                  | Access | Purpose                                                                 |
| ------------------------------ | ------ | ----------------------------------------------------------------------- |
| `POST /documents/upload`       | Admin  | Multipart upload. Validates type/size, stores file, creates row, kicks off indexing (`pending`→`processing`). |
| `GET /documents`               | Admin  | List active documents (`status = 1`) with metadata + indexing status.   |
| `GET /documents/{id}`          | Admin  | Fetch one active document (404 if missing/deleted).                      |
| `DELETE /documents/{id}`       | Admin  | **Soft delete** (`status = 5`) + remove its vectors from the store.      |
| `POST /documents/{id}/reindex` | Admin  | Re-run the indexing pipeline for one document.                          |

### Search & Chat (all Public)

| Method & Path        | Access | Purpose                                                                          |
| -------------------- | ------ | ------------------------------------------------------------------------------- |
| `GET /health`        | Public | Liveness probe (`{status:"ok"}`).                                                |
| `POST /search`       | Public | Embed query, vector-similarity search, return top-k ranked chunks (`document_name`, `excerpt`, `score`). |
| `POST /chat/query`   | Public | RAG (non-streaming): retrieve → prompt → LLM → persist turn → `{answer, sources, conversation_id, message_id}`. |
| `POST /chat/stream`  | Public | RAG over **Server-Sent Events** (`text/event-stream`): streams `data: {"delta"}` frames, then a terminal `data: {"done", sources, conversation_id, message_id}`; persists the turn. |
| `GET /chat/conversations`        | Public | List active conversations, newest first (`{items, total}`).            |
| `GET /chat/conversations/{id}`   | Public | One active conversation + its chronological active messages (404 if missing). |
| `DELETE /chat/conversations/{id}`| Public | **Soft delete** (`status = 5`) the conversation and its messages.      |

### Admin (all Admin)

| Method & Path                | Access | Purpose                                                                       |
| ---------------------------- | ------ | ----------------------------------------------------------------------------- |
| `GET /admin/stats`           | Admin  | Total documents, total chunks, indexed vs failed counts, vector DB health.     |
| `GET /admin/indexing-status` | Admin  | Per-document / per-status indexing breakdown.                                  |
| `POST /admin/reindex`        | Admin  | Bulk re-index (all, or by `document_id`).                                      |
| `GET /admin/health`          | Admin  | DB connectivity + vector store reachability.                                   |

---

## 8a. Chat Streaming + Persistence Pipeline

Both chat endpoints share the same RAG core (retrieve → build grounded prompt →
generate) in `chat_service`, and both persist the turn. They differ only in how the
answer is delivered.

### Conversation resolution & persistence

For every turn (`query` and `stream_answer`):

1. **Resolve or create** the conversation — `resolve_or_create_conversation` reuses an
   active conversation when a valid `conversation_id` is supplied, otherwise creates a
   new one (title derived from the first user message) and commits so its id is available.
2. **Persist the user message** *before* generating.
3. **Load history from the DB** (the source of truth), dropping the just-saved user turn
   so the prompt's explicit `Question:` line is not duplicated; a client-supplied
   `history` is only used as a fallback when the DB has no prior turns.
4. Retrieve context, build the prompt, and generate.
5. **Persist the assistant message** *after* generating, with its `sources` JSONB.

### SSE generator (`POST /chat/stream`)

The router wraps `chat_service.stream_answer(...)` in a `StreamingResponse`
(`media_type="text/event-stream"`, with `Cache-Control: no-cache` and
`X-Accel-Buffering: no` so frames flush through dev servers/proxies).

Key design point: **the generator owns its own `SessionLocal()` session** rather than a
`Depends(get_db)` one — the request-scoped session is already closed by the time the
streaming body runs. `stream_answer` opens the session, persists the user message,
streams deltas, persists the assistant message, and closes the session in `finally`.

`stream_answer` yields `(kind, value)` tuples the router maps to SSE frames:

| Yielded tuple                                              | SSE frame                                                             |
| --------------------------------------------------------- | -------------------------------------------------------------------- |
| `("delta", text)` (many)                                  | `data: {"delta": text}`                                              |
| `("done", {sources, conversation_id, message_id})` (once) | `data: {"done": true, "sources": [...], "conversation_id": N, "message_id": N}` |
| `("error", message)` (on failure)                         | `data: {"error": message}`                                          |

The generator **never raises**: on any exception it rolls back and emits an `error`
frame so the client gets a clean SSE end instead of a 500.

### Streaming behaviour by provider

Token delivery goes through `LLMProvider.generate_stream`, which self-falls back to a
local word-stream on transport/rate-limit errors; `_stream_answer_deltas` is a second
safety net for unexpected failures.

- **`local` (default, no key)** — the extractive answerer composes the grounded answer
  and streams it by chunking the text server-side (simulated token streaming).
- **`openai` (`gpt-4o-mini`)** — native token streaming from the OpenAI API.
- **`anthropic` (`claude-opus-4-8`)** — native token streaming from the Anthropic API.

A selected provider with a missing key falls back to `local`. Anthropic has no public
embeddings API, so `EMBEDDING_PROVIDER=anthropic` falls back to the local hashing
embedder (use `EMBEDDING_PROVIDER=openai` with `OPENAI_API_KEY` for hosted embeddings).

---

## 9. Milestone / Implementation Plan

Mapped to the build phases in `REQUIREMENTS.md` §10.

1. **Foundation** — Scaffold tree, `config.py` (pydantic-settings), `database/`
   (SQLAlchemy engine/session, PostgreSQL via psycopg v3), Alembic migrations +
   `scripts/pg_local.sh`, `models/base.py` (`Base` + `TimestampStatusMixin`), logging +
   error handlers, `FastAPI` app skeleton, `/admin/health`. Acceptance: app boots and
   `health` passes with no keys.
2. **Document Management** — `Document`/`Chunk` models, schemas, `file_validation`,
   `extractor`/`cleaner` for PDF/DOCX/TXT/MD, `document_service`, document API routes
   (upload/list/get/soft-delete). Acceptance: upload + soft delete work.
3. **Embedding & Indexing** — Provider interfaces + factory, local embedder (no torch),
   OpenAI/Anthropic embedders, `chunker`, `vectorstore` (pgvector), `pipeline`, indexing
   status lifecycle, `/documents/{id}/reindex`. Acceptance: documents reach `indexed`.
4. **Semantic Search** — `search_service` + `POST /search` with configurable `TOP_K` and
   similarity ranking. Acceptance: relevant chunks returned, ranked.
5. **RAG Chat** — LLM provider impls + local answerer, `prompt` builder, `chat_service`,
   `POST /chat/query` returning grounded answer + sources. Acceptance: grounded answers
   with traceable sources.
6. **Admin** — `admin_service` + stats / indexing-status / reindex endpoints. Acceptance:
   admin stats and bulk reindex work.
7. **Tests** — `pytest` suite (32 tests) against `ragdb_test` (Postgres + pgvector) +
   local providers; covers document lifecycle, search, chat, admin, status filtering, soft
   delete. A live end-to-end smoke check lives at `scripts/e2e_smoke.sh`. Acceptance: green
   suite with **no external keys**.
8. **Frontend** — Minimal Next.js POC: upload, chat, search results, source references.
   Acceptance: end-to-end against local backend.
9. **Validation** — Verify all `DONE_CRITERIA.md` items: runs against the local
   `pg_local.sh` Postgres + pgvector instance and against any external Postgres via
   `DATABASE_URL`; `alembic upgrade head` applies the schema; upload/index/search/RAG/
   sources all work; `.env.example` and README setup documented.

---

## 10. Auth & Route Protection

The system has two surfaces: a **public chat** (`/` in the frontend) and a
**login-gated admin panel** (`/admin`). Authentication is a deliberately small
single-admin JWT scheme — there is **no users table**.

### Components

- **Config (`app/core/config.py`)** — the admin credentials and JWT parameters are plain
  settings: `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `JWT_SECRET_KEY`, `JWT_ALGORITHM`,
  `ACCESS_TOKEN_EXPIRE_MINUTES`. Dev defaults let tests/local dev run with zero extra env;
  they are insecure and must be overridden in production. Read via `get_settings()` at call
  time so test/env overrides are honored.
- **Security utilities (`app/core/security.py`)** — PyJWT (HS256) helpers and the FastAPI
  dependency:
  - `create_access_token(subject)` — signs a JWT with `sub` (the admin username) and `exp`
    (now + `ACCESS_TOKEN_EXPIRE_MINUTES`).
  - `authenticate_admin(username, password)` — **constant-time** (`secrets.compare_digest`)
    comparison against the configured admin, to avoid timing leaks.
  - `decode_token(token)` — verifies signature/expiry; raises `jwt.PyJWTError` on failure.
  - `get_current_admin(token = Depends(oauth2_scheme))` — the protection dependency. Decodes
    the bearer token (via `OAuth2PasswordBearer(tokenUrl="auth/login")`), and rejects any
    invalid/expired token, or one whose `sub` is not the configured `ADMIN_USERNAME`, with a
    `401` carrying `WWW-Authenticate: Bearer`. On success returns `{"username": ...}`.
- **Auth routes (`app/api/auth.py`)** — OAuth2 *password* flow:
  - `POST /auth/login` (**public**) takes `OAuth2PasswordRequestForm` (form-encoded
    `username`+`password`); on valid creds returns `{access_token, token_type:"bearer"}`,
    else `401`.
  - `GET /auth/me` (**protected**, `Depends(get_current_admin)`) returns the admin identity.

### Route-protection matrix (router-level dependency)

Protection is applied **per router**, not per endpoint, so it cannot be forgotten on a new
route:

- `app/api/documents.py` — `APIRouter(prefix="/documents", dependencies=[Depends(get_current_admin)])`.
- `app/api/admin.py` — `APIRouter(prefix="/admin", dependencies=[Depends(get_current_admin)])`.

Every route under `/documents*` and `/admin/*` therefore requires a valid admin JWT, plus
`GET /auth/me`. Everything else is public: `GET /health`, `POST /auth/login`, `POST /search`,
and all chat routes (`POST /chat/query`, `POST /chat/stream`, and the `GET`/`GET {id}`/`DELETE`
`/chat/conversations` endpoints — conversation history is part of the public chat). See the
§8 API tables for the full Access column.

### Frontend integration

`frontend/services/api.ts` posts form-encoded creds to `/auth/login`, stores the returned
token in `localStorage` (`admin_token`), and `authedFetch` injects
`Authorization: Bearer <token>` on every protected call — clearing the token and throwing on
any `401`. `frontend/app/admin/page.tsx` renders a login form until a token is present, then
shows the document-management UI and dashboard; the public chat page (`/`) calls no authed
endpoints.

---

## 11. Security Notes

- **File validation** — Enforce `ALLOWED_EXTENSIONS` and `MAX_UPLOAD_MB` before persisting;
  verify extension against detected MIME/content; sanitize filenames (strip path
  components, prevent traversal); store under a controlled `uploads/` dir with generated
  names; never execute uploaded content.
- **Input sanitization** — Validate/clamp all request bodies via Pydantic (e.g. `TOP_K`
  bounds, query length limits). Sanitize text used in prompts to mitigate prompt injection;
  the local answerer stays strictly extractive over retrieved context.
- **Secret protection** — API keys, `ADMIN_PASSWORD`, and `JWT_SECRET_KEY` only via env /
  `pydantic-settings`; never hardcoded, never logged, not returned by any endpoint; `.env`
  gitignored, `.env.example` documents names only.
- **Admin auth** — single-admin JWT (HS256), constant-time credential check, router-level
  `get_current_admin` on `/documents*` and `/admin/*` (see §10). The dev defaults for
  `ADMIN_PASSWORD` / `JWT_SECRET_KEY` are insecure and **must** be overridden in production —
  anyone with the JWT secret can forge admin tokens.
- **LLM rate-limit handling** — Provider calls wrap external APIs with timeouts, retry with
  exponential backoff on 429/5xx, and translate exhausted retries into a clean
  `RateLimitError` → HTTP 429/503. If a provider is unavailable, the system degrades to the
  local fallback rather than failing hard.
- **Error hygiene** — Domain exceptions produce consistent JSON; internal details and
  stack traces are logged server-side but not leaked to clients.
