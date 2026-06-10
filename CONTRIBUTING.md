# Contributing & Coding Standards

This guide defines the conventions for the **RAG Knowledge Base** project. It
covers both stacks — the Python/FastAPI backend (`backend/`) and the
TypeScript/Next.js 14 frontend (`frontend/`) — and the local PostgreSQL +
pgvector development flow. Please read this before opening a pull request.

The goal is consistency: linters and formatters are configured to **fit the
existing code**, so running them should normalize style, not rewrite the
codebase.

---

## Table of contents

- [Project layout](#project-layout)
- [Backend architecture (layered)](#backend-architecture-layered)
- [Backend conventions](#backend-conventions)
  - [Naming](#naming)
  - [Type hints & docstrings](#type-hints--docstrings)
  - [Error handling & logging](#error-handling--logging)
  - [Common database field contract](#common-database-field-contract)
- [Frontend conventions](#frontend-conventions)
- [Local development](#local-development)
  - [PostgreSQL + pgvector dev flow](#postgresql--pgvector-dev-flow)
  - [Running the backend](#running-the-backend)
  - [Running the frontend](#running-the-frontend)
  - [Running the tests](#running-the-tests)
- [Lint & format](#lint--format)

---

## Project layout

```
RAG Chatbot/
├── backend/            FastAPI service (Python 3.12)
│   ├── app/            Application package (see layered architecture below)
│   ├── migrations/     Alembic migrations (managed schema)
│   ├── scripts/        pg_local.sh, e2e_smoke.sh
│   ├── tests/          pytest suite
│   ├── pyproject.toml  pytest + ruff + black config
│   ├── requirements.txt        runtime deps
│   └── requirements-dev.txt    lint/format deps (ruff, black)
└── frontend/           Next.js 14 App Router (TypeScript)
    ├── app/            Routes, layout, global styles
    ├── components/     React components
    └── services/       Typed API client
```

---

## Backend architecture (layered)

The backend follows a strict, one-directional layered architecture. Each layer
depends only on the layers below it; never reach "up" or sideways across a peer.

```
api  →  services  →  rag / vectorstore / database
                          │
        schemas ──────────┤  (request/response contracts)
        models  ──────────┤  (ORM rows)
        utils   ──────────┘  (cross-cutting helpers)
```

| Layer (`app/…`)  | Responsibility |
| ---------------- | -------------- |
| `api/`           | FastAPI routers. **Thin.** Parse/validate input via `schemas`, call a `service`, map the result back to a `schema`. No business logic, no direct DB queries. |
| `services/`      | Business logic and orchestration (document lifecycle, indexing, search, chat). The only layer that coordinates `database`, `rag`, and `vectorstore`. |
| `rag/`           | Retrieval-augmented-generation primitives: embeddings, LLM client, prompt assembly. Provider-facing. |
| `vectorstore/`   | Vector storage abstraction (`base.py` interface + `pgvector_store.py` implementation). Pluggable behind the `base` protocol. |
| `database/`      | SQLAlchemy engine/session (`session.py`) and the declarative `Base`, `Status` enum, and `TimestampStatusMixin` (`base.py`). |
| `models/`        | SQLAlchemy ORM models (`Document`, `Chunk`). One file per table. Registered in `models/__init__.py` so `init_db()` sees them. |
| `schemas/`       | Pydantic request/response models. The public API contract; mirror these in the frontend `services/api.ts` types. |
| `utils/`         | Cross-cutting helpers: domain `errors`, file validation (`files`), structured `logging`. Imported by any layer. |
| `core/`          | App configuration (`config.py`, pydantic-settings). |

Rules of thumb:

- **Keep routers thin.** If a router has branching logic or DB queries, push it
  into a service.
- **Services own transactions.** Services call `db.commit()` / `db.refresh()`;
  the API layer does not.
- **Never import an `api` module from a `service`** (or any lower layer). If you
  feel the urge, the abstraction belongs in `utils` or `schemas`.
- **Vector store access goes through `vectorstore.base`,** not the concrete
  pgvector implementation, so the store stays swappable.

---

## Backend conventions

Target runtime is **Python 3.12**. All new modules start with
`from __future__ import annotations`.

### Naming

- `snake_case` for modules, functions, variables.
- `PascalCase` for classes (ORM models, Pydantic schemas, exceptions).
- `UPPER_SNAKE_CASE` for module-level constants (e.g. `UPLOADS_DIR`).
- Leading underscore for private module helpers (`_store_bytes`,
  `_ensure_uploads_dir`).
- Service functions read as verbs of the domain action:
  `create_document`, `soft_delete_document`, `set_indexing_status`,
  `get_document_or_404`.
- ORM model files are singular (`document.py` → `Document`, table `documents`).

### Type hints & docstrings

- **Full type hints on every public function** — parameters and return type.
  Use modern syntax enabled by `from __future__ import annotations`:
  `str | None`, `list[Document]`, `tuple[Path, str]`.
- **Module docstring** at the top of every module describing its role.
- **One-line (or short) docstring** on every public function/class/method,
  written as an imperative summary
  (e.g. `"""Validate, store, and create a new document row..."""`).
- Use `# pragma: no cover` on debug-only helpers like `__repr__`, and targeted
  `# noqa: <code>` only where a lint rule is intentionally violated (see the
  `BLE001` example in `services/document_service.py`).

### Error handling & logging

- Raise **domain exceptions** from `app/utils/errors.py`, never bare
  `HTTPException` in services. The hierarchy maps to HTTP status codes:

  | Exception | Status |
  | --------- | ------ |
  | `AppError` (base) | 400 |
  | `NotFoundError` | 404 |
  | `ValidationError` | 422 |
  | `UnsupportedFileTypeError` | 415 |
  | `ProviderError` | 502 |
  | `RateLimitError` | 429 |

- All exceptions are caught by handlers registered via
  `register_exception_handlers(app)` and rendered as a consistent
  `{"detail": ...}` JSON body. **Internal details are logged, never leaked** to
  clients (the catch-all handler returns `"Internal server error"`).
- **Logging** uses `app.utils.logging.get_logger(__name__)` — never the bare
  `logging` module, and never `print`. Logging is configured once, idempotently,
  with level from `LOG_LEVEL` (default `INFO`).
- Use lazy `%`-style log args (`logger.info("Created document id=%s", id)`),
  not f-strings, so disabled levels cost nothing.
- **Never log secrets.** Cleanup/best-effort code (e.g. vector cleanup during a
  soft delete) logs and swallows errors so the primary operation still succeeds.

### Common database field contract

Every table reuses `Base` **and** `TimestampStatusMixin` from
`app/database/base.py`, which provides four common columns on every row:

| Column | Type | Rule |
| ------ | ---- | ---- |
| `id` | `BIGINT` PK, autoincrement (`BIGSERIAL`) | Surrogate primary key. |
| `status` | `SMALLINT`, NOT NULL, default `1` | Lifecycle / soft-delete flag. |
| `added_time` | `timestamptz`, NOT NULL, default `now()` | Creation time. |
| `last_modified` | `timestamptz`, NOT NULL, default `now()`, `onupdate=now()` | Auto-bumped on update. |

The `status` value uses the `Status` IntEnum:

```python
class Status(IntEnum):
    INACTIVE = 0
    ACTIVE = 1
    DELETED = 5
```

Contract for working with rows:

- **Soft delete only.** To delete, set `status = Status.DELETED.value`; do not
  issue SQL `DELETE`. See `soft_delete_document`.
- **Active-only reads by default.** Queries filter
  `where(<Model>.status == Status.ACTIVE.value)` unless an explicit
  `include_inactive=True` flag is passed. See `list_documents` / `get_document`.
- **Provide a `get_*_or_404` helper** per entity that raises `NotFoundError`
  when an active row is missing.
- New tables must extend `TimestampStatusMixin` rather than redefining these
  columns, and must be imported in `app/models/__init__.py` so they register on
  `Base.metadata`.
- Schema changes are applied through **Alembic migrations** (`alembic upgrade
  head`), not by editing the database by hand.

---

## Frontend conventions

Target is **Next.js 14 (App Router) + React 18 + TypeScript 5** in `strict`
mode. The style below matches the existing code and is enforced by Prettier +
ESLint.

- **Formatting** (Prettier, `.prettierrc`): 2-space indent, **double quotes**,
  semicolons, trailing commas (`all`), `printWidth: 100`.
- **`"use client"`** at the top of any component using hooks/state/events;
  server components are the default.
- **Components** are `PascalCase` and live in `components/` (one component per
  file, default export). Routes/layout live in `app/`.
- **Imports** use the `@/*` path alias (e.g. `@/services/api`,
  `@/components/Sources`).
- **Types** for backend payloads are declared as `interface`s in
  `services/api.ts` and must mirror the backend Pydantic `schemas`. Keep them in
  sync when the API changes.
- **API access** goes through the typed helpers in `services/api.ts`. Those
  helpers throw an `Error` carrying the backend `detail` message on non-OK
  responses; UI components catch and surface it (`e instanceof Error ? ...`).
- The backend base URL comes from `NEXT_PUBLIC_API_BASE_URL`
  (default `http://127.0.0.1:8000`).
- Prefer `useCallback` for event handlers passed to children, and `void
  someAsync()` when intentionally not awaiting in an event handler.

ESLint extends `next/core-web-vitals` and `prettier` (so Prettier owns
formatting). A few strict rules are set to `warn` rather than `error`
(`no-img-element`, `no-unescaped-entities`, `exhaustive-deps`) so existing
components pass — fix warnings when you touch the surrounding code.

---

## Local development

### PostgreSQL + pgvector dev flow

No system PostgreSQL install is required. A self-contained, no-sudo userspace
**PostgreSQL 16 + pgvector** instance is provisioned by
`backend/scripts/pg_local.sh` and runs on `127.0.0.1:54329`.

```bash
cd backend
bash scripts/pg_local.sh up      # provision & start local Postgres + pgvector
alembic upgrade head             # apply managed schema (extension, tables, HNSW index)
```

`alembic upgrade head` creates the `vector` extension, the `documents` and
`chunks` tables, the `chunks.embedding` `Vector(384)` column, and the HNSW
cosine ANN index.

Other subcommands: `bash scripts/pg_local.sh status | psql | url | reset |
stop`. Cluster data lives under `$HOME/.cache/rag-knowledge-base/pg` (off the
repo, because Windows drvfs can't hold Postgres's `0700` perms); override with
`RAG_PG_HOME`. For production, point `DATABASE_URL` at any PostgreSQL server
with `pgvector` available and run `alembic upgrade head`.

### Running the backend

```bash
cd backend
python -m venv venv && source venv/bin/activate   # first time only
pip install -r requirements.txt
uvicorn app.main:app --reload
```

API: `http://127.0.0.1:8000` — Swagger UI at `http://127.0.0.1:8000/docs`.

### Running the frontend

```bash
cd frontend
npm install
npm run dev
```

App: `http://localhost:3000`.

### Running the tests

The pytest suite runs against the `ragdb_test` database, so `pg_local.sh up`
must have run first:

```bash
cd backend
DATABASE_URL=postgresql+psycopg://raguser@127.0.0.1:54329/ragdb_test \
  ./venv/bin/python -m pytest -q
```

For a full upload → index → search → chat smoke check:
`bash backend/scripts/e2e_smoke.sh`.

---

## Lint & format

Run these before pushing. Tooling is configured to fit the existing code, so a
clean working tree should stay clean.

### Backend (ruff + black)

Install the dev tools (separate from runtime deps):

```bash
cd backend
pip install -r requirements-dev.txt
```

Config lives in `backend/pyproject.toml`:

- **black** — line length 100, target `py312`.
- **ruff** — line length 100, rule set `E, F, I, UP, B` (with `E203`/`E501`
  delegated to black). Per-file ignores relax `migrations/*` and let
  `**/__init__.py` re-export without `F401`.

Commands:

```bash
ruff check . --fix     # lint + autofix (imports, modern syntax, etc.)
ruff check .           # lint only (CI)
black .                # format
black --check .        # verify formatting (CI)
```

Recommended order on a fresh checkout: `black .` then `ruff check . --fix`.

### Frontend (ESLint + Prettier)

Config: `frontend/.eslintrc.json`, `frontend/.prettierrc`,
`frontend/.prettierignore`. Commands (from `frontend/`):

```bash
npm run lint           # next lint (ESLint)
npm run format         # prettier --write .
npm run format:check   # prettier --check . (CI)
```

> Note: `next.config.mjs` sets `eslint.ignoreDuringBuilds: true` so a missing
> lint pass won't block `next build`; run `npm run lint` explicitly in CI.
