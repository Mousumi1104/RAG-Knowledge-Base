# Deployment

This document covers running the full RAG Knowledge Base stack
(PostgreSQL + pgvector, FastAPI backend, Next.js frontend) with Docker Compose,
and the production hardening checklist.

> The local-dev workflow (`Makefile`, `backend/scripts/pg_local.sh`,
> `backend/.env`) is unchanged and independent of everything below.

## Quick start

```bash
# 1. Create your deployment env from the template.
cp .env.example .env
# 2. Edit .env — at minimum set GEMINI_API_KEY (see below).
# 3. Build and start the whole stack.
docker compose up --build
```

Once up:

- Backend API: <http://localhost:8000> (health: <http://localhost:8000/health>)
- Frontend UI: <http://localhost:3000>

## Services

| Service    | Image / build        | Port  | Notes                                              |
| ---------- | -------------------- | ----- | -------------------------------------------------- |
| `db`       | `pgvector/pgvector:pg16` | (internal) | user `raguser`, db `ragdb`, named volume `pgdata`, `pg_isready` healthcheck |
| `backend`  | `./backend`          | 8000  | runs `alembic upgrade head` then uvicorn; uploads on named volume |
| `frontend` | `./frontend`         | 3000  | Next.js 14 production build (`next start`)         |

## Required environment (`.env`)

Compose reads `${VAR}` references from a root `.env` file. Copy `.env.example`
and fill in:

| Variable                  | Purpose                                                | Prod note                              |
| ------------------------- | ------------------------------------------------------ | -------------------------------------- |
| `POSTGRES_PASSWORD`       | Password for the `raguser` role in the db container    | Change from the `ragpass` default      |
| `GEMINI_API_KEY`          | Google AI Studio key for the Gemini LLM provider       | Required for chat; never commit it     |
| `ADMIN_USERNAME`          | Admin login username                                   | Set a non-default value                |
| `ADMIN_PASSWORD`          | Admin login password                                   | **Change** from `admin123`             |
| `JWT_SECRET_KEY`          | HS256 signing secret for auth tokens                   | **Change** to a long random string     |
| `NEXT_PUBLIC_API_BASE_URL`| Browser-facing API URL (inlined at build time)         | Set to the public API URL              |

### Setting `GEMINI_API_KEY`

The provider policy is fixed: `EMBEDDING_PROVIDER=local` (384-dim hash embedder,
matching the `chunks.embedding vector(384)` column) and `LLM_PROVIDER=gemini`
with `GEMINI_MODEL=gemini-2.5-flash`. Get a free key from
<https://aistudio.google.com/apikey> and put it in `.env`:

```dotenv
GEMINI_API_KEY=your-key-here
```

The key is **never** hardcoded in Dockerfiles, compose, or CI — it is only
referenced as `${GEMINI_API_KEY}` and injected at runtime. CI does not need it
(tests run with the deterministic `local` LLM provider).

## Migrations run on boot

The backend container's entrypoint (`backend/entrypoint.sh`) runs
`alembic upgrade head` **before** starting uvicorn. Alembic resolves the
target DB from `DATABASE_URL` (see `backend/migrations/env.py`). This is
idempotent — already-applied revisions are skipped — so the pgvector extension,
tables, and HNSW index are always in lockstep with the code, with no separate
migration step.

## Compose vs. local DB host (important)

The DB hostname differs between the two workflows — do not mix them up:

| Workflow                  | `DATABASE_URL` host                                   |
| ------------------------- | ----------------------------------------------------- |
| Docker Compose            | `postgresql+psycopg://raguser:***@db:5432/ragdb`      |
| Local dev (`pg_local.sh`) | `postgresql+psycopg://raguser@127.0.0.1:54329/ragdb`  |

Inside the compose network, services resolve each other by name, so the backend
reaches Postgres at `db:5432`. The `127.0.0.1:54329` value lives only in
`backend/.env` for local development and is untouched by the deployment layer.

## Frontend API URL is build-time (Next 14)

`NEXT_PUBLIC_*` variables are **inlined into the client bundle at build time**,
not read at runtime. `NEXT_PUBLIC_API_BASE_URL` is wired through a Docker build
arg (default `http://localhost:8000`). To change the API URL you must rebuild:

```bash
NEXT_PUBLIC_API_BASE_URL=https://api.example.com docker compose up --build
```

## Production hardening checklist

- [ ] **Change `JWT_SECRET_KEY`** to a long random value (`openssl rand -hex 32`).
- [ ] **Change `ADMIN_PASSWORD`** (and ideally `ADMIN_USERNAME`) from the dev defaults.
- [ ] **Change `POSTGRES_PASSWORD`** from `ragpass`.
- [ ] **Set real `CORS_ORIGINS`** in `docker-compose.yml` to your actual frontend origin(s) instead of `localhost`.
- [ ] **Set `NEXT_PUBLIC_API_BASE_URL`** to the public HTTPS API URL and rebuild the frontend.
- [ ] **Use a managed / persistent Postgres with pgvector** (e.g. a managed PG with the `vector` extension) rather than the bundled container for durability and backups.
- [ ] Put the API and UI behind TLS (reverse proxy / load balancer).
- [ ] Keep `GEMINI_API_KEY` (and all secrets) in a secrets manager — never in committed files.

## CI

`.github/workflows/ci.yml` runs on every push / pull request:

- **backend**: spins up a `pgvector/pgvector:pg16` service container
  (published on host port `54329` to match `tests/conftest.py`), installs
  `requirements.txt` + `requirements-dev.txt`, runs `ruff check` and
  `black --check`, applies `alembic upgrade head`, then `pytest -q` with
  `EMBEDDING_PROVIDER=local` and `LLM_PROVIDER=local` (no Gemini key needed).
- **frontend**: `npm ci`, `npm run lint`, `npm run build` on Node 20.
