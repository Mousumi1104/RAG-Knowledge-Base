# Project Handover — AI-Powered RAG Knowledge Base / Chatbot

This is the engineering handover for the next owner of this project. It is accurate
to the code as of 2026-06-05. Where this doc and older docs disagree on the active
LLM provider, **this doc is authoritative** (the running config now uses Gemini, see
[Provider policy](#provider-policy)).

Companion docs: `README.md` (feature/API reference), `backend/ARCHITECTURE.md`
(design authority), `DEPLOYMENT.md` (Docker + prod), `DAY1_SETUP_REPORT.md` (Day-1
status), `CONTRIBUTING.md` (standards/linters).

---

## 1. System overview

A Retrieval-Augmented Generation knowledge base with two surfaces:

- **`/` — public chat** (no login). Natural-language questions answered from indexed
  documents, with token-by-token streaming and source references. History is
  persisted in Postgres and survives refresh/restart.
- **`/admin` — login-gated panel** (single-admin JWT). Upload / list / delete /
  re-index documents and view the indexing & health dashboard.

**Stack**

| Layer     | Tech                                                                 |
| --------- | ------------------------------------------------------------------- |
| Backend   | FastAPI, SQLAlchemy 2.0, Alembic, psycopg v3, Python 3.10+          |
| Vector DB | **pgvector** — `chunks.embedding vector(384)` + HNSW cosine index   |
| Database  | PostgreSQL (relational + vectors in one DB)                         |
| Frontend  | Next.js 14, React, TypeScript                                       |
| Embedding | **local** deterministic hashing embedder (384-d, no API key)        |
| LLM       | **Gemini** (`gemini-2.5-flash`); local extractive fallback          |

The backend is strictly layered: `api/` (thin routers) → `services/` (business logic)
→ `rag/` + `vectorstore/` + `database/` + providers → `models/`/`schemas/`/`utils/`.

---

## 2. Architecture & data flow

**Indexing (admin):**

```
upload → validate (ext/size) → store under uploads/ → extract text
       → clean/normalize → chunk (CHUNK_SIZE/CHUNK_OVERLAP)
       → embed (LOCAL 384-d hashing) → store in pgvector (chunks.embedding)
status: pending → processing → indexed | failed
```

**Chat / search (public):**

```
question → embed query (LOCAL 384-d) → pgvector ANN cosine search (top_k)
        → drop hits below relevance threshold (0.1)
        → build strictly-grounded prompt (context + question [+ history])
        → LLM (Gemini) generates answer → return {answer, sources}
        → persist user + assistant messages (+ sources JSONB)
```

Tables: `documents`, `chunks` (with the `vector(384)` column), `conversations`,
`messages`. Common fields on every table: `id`, `status` (0/1/5), `added_time`,
`last_modified`. Deletes are **soft deletes** (`status = 5`); document delete also
clears its vectors. Schema is created/versioned by Alembic
(`backend/migrations/versions/`), not by `create_all`.

Key source files:
- Provider selection/fallback: `backend/app/rag/llm.py`, `backend/app/rag/embeddings.py`
- RAG flow + persistence: `backend/app/services/chat_service.py`
- Search: `backend/app/services/search_service.py`
- Indexing pipeline: `backend/app/services/indexing_service.py`
- Extraction: `backend/app/services/extraction.py`
- Config: `backend/app/core/config.py`; auth: `backend/app/core/security.py`

---

## 3. Provider policy

> **Do not change these without understanding the consequences.**

| Setting              | Value                | Why it is fixed                                                                 |
| -------------------- | -------------------- | ------------------------------------------------------------------------------- |
| `EMBEDDING_PROVIDER` | `local`              | The `chunks.embedding` column is `vector(384)` to match the local hash embedder. Switching the embedder changes the vector space **and dimension**, which breaks retrieval and requires a full re-index (and possibly a migration). |
| `LLM_PROVIDER`       | `gemini`             | Current answer generator.                                                       |
| `GEMINI_MODEL`       | `gemini-2.5-flash`   | A *thinking* model. `GeminiLLMProvider` sets `generationConfig.thinkingConfig.thinkingBudget = 0` — without this, thinking can consume the entire output budget and return empty text. |
| `GEMINI_API_KEY`     | (env only)           | **Must** be supplied via environment / secrets manager. Never hardcode it. If missing, the system logs a warning and **falls back to the local extractive LLM** (so chat still answers, just without Gemini). |

The Gemini key is sent in the `x-goog-api-key` header (kept out of URLs/logs). OpenAI
and Anthropic providers also exist in code and are selectable, but are **not** the
current policy.

---

## 4. How to run locally

The local dev DB is a no-sudo userspace **Postgres 16 + pgvector** on
`127.0.0.1:54329`, provisioned by `backend/scripts/pg_local.sh`. It does **not**
auto-start — bring it up first. Data lives under `$HOME/.cache/rag-knowledge-base/pg`
(override with `RAG_PG_HOME`).

### With `make` (preferred)

```bash
make up        # start local Postgres (:54329) + alembic upgrade head
make backend   # uvicorn --reload on 127.0.0.1:8000
make frontend  # Next.js dev server on :3000
make verify    # full-stack verification (see §6)
make test      # backend pytest against ragdb_test
make lint      # ruff + black + eslint
```

### Raw commands (if `make` is unavailable)

```bash
bash backend/scripts/pg_local.sh up                 # local Postgres + pgvector :54329
cd backend && alembic upgrade head                  # apply schema
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --env-file .env
cd frontend && npm install && npm run dev           # http://localhost:3000
bash backend/scripts/pg_local.sh stop               # stop the DB when done
```

Backend: <http://127.0.0.1:8000> (Swagger at `/docs`). To use Gemini locally, set
`LLM_PROVIDER=gemini` + `GEMINI_API_KEY` in `backend/.env` and restart the backend.

> **WSL note:** on a `/mnt/...` Windows drive, `npm run dev` is very slow (recompiles
> per navigation). Use `npm run build && npm run start` (or `make frontend-prod`) for
> a fast experience; for hot-reload dev keep the repo on the Linux filesystem.

### Via Docker Compose (full stack)

```bash
cp .env.example .env        # then edit: set GEMINI_API_KEY + secrets (see §5)
docker compose up --build
```

Services: `db` (`pgvector/pgvector:pg16`), `backend` (runs `alembic upgrade head`
then uvicorn, port 8000), `frontend` (Next.js prod build, port 3000). Inside the
compose network the DB host is `db:5432` — **not** `127.0.0.1:54329` (that value is
local-dev only; do not mix them). `NEXT_PUBLIC_API_BASE_URL` is inlined at **build
time**, so changing the public API URL requires a rebuild.

---

## 5. Environment variables

Backend reads from `backend/.env`; Docker Compose reads `${VAR}` from a root `.env`
(template: `.env.example`). All have working dev defaults except the Gemini key.

| Variable                      | Purpose                                   | Dev default                                          | Change in prod?                                  |
| ----------------------------- | ----------------------------------------- | ---------------------------------------------------- | ------------------------------------------------ |
| `DATABASE_URL`                | SQLAlchemy URL (psycopg v3)               | `postgresql+psycopg://raguser@127.0.0.1:54329/ragdb` | Point at managed Postgres **with pgvector**      |
| `VECTOR_DB`                   | Vector store impl                         | `pgvector`                                           | Keep                                             |
| `EMBEDDING_PROVIDER`          | Embedder                                  | `local`                                              | **Keep `local`** (see §3 — 384-d coupling)       |
| `LLM_PROVIDER`                | Answer generator                          | `gemini` (running) / `local` (code default)          | `gemini`                                         |
| `GEMINI_API_KEY`              | Google AI Studio key                      | (empty)                                              | **Required** — supply via env/secrets, never commit |
| `GEMINI_MODEL`                | Gemini model                              | `gemini-2.5-flash`                                   | Keep (thinking-budget=0 is wired for this class) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Optional alt providers           | (empty)                                              | Unused under current policy                      |
| `EMBEDDING_DIM`               | Vector dimension                          | `384`                                                | **Keep 384** (matches the DB column)             |
| `CHUNK_SIZE` / `CHUNK_OVERLAP`| Chunking params                           | `1000` / `200`                                       | Tune; re-index after changing                    |
| `TOP_K`                       | Default chunks retrieved                  | `5`                                                  | Tune                                             |
| `MAX_UPLOAD_MB`               | Max upload size                           | `25`                                                 | As needed                                        |
| `ALLOWED_EXTENSIONS`          | Permitted types                           | `pdf,docx,txt,md`                                    | As needed                                        |
| `CORS_ORIGINS`                | Allowed frontend origins                  | `http://localhost:3000`                              | **Set real origin(s)**                           |
| `ADMIN_USERNAME`              | Admin login                               | `admin`                                              | Set non-default                                  |
| `ADMIN_PASSWORD`              | Admin password                            | `admin123`                                           | **Change — strong value**                        |
| `JWT_SECRET_KEY`              | HS256 signing secret                      | `dev-secret-change-me-in-production`                 | **Change — `openssl rand -hex 32`**              |
| `JWT_ALGORITHM`               | JWT algorithm                             | `HS256`                                              | Keep                                             |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime                            | `720` (12h)                                          | As needed                                        |
| `POSTGRES_PASSWORD`           | (compose `db`) `raguser` password         | `ragpass`                                            | **Change**                                       |
| `NEXT_PUBLIC_API_BASE_URL`    | Browser-facing API URL (build-time)       | `http://127.0.0.1:8000`                              | Public HTTPS API URL; **rebuild frontend**       |

Anyone who learns `JWT_SECRET_KEY` can forge admin tokens — treat it as a top secret.

---

## 6. Testing

| Check                | Command                                                                                          | Notes                                                      |
| -------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------- |
| Backend unit/API     | `cd backend && DATABASE_URL=postgresql+psycopg://raguser@127.0.0.1:54329/ragdb_test ./venv/bin/python -m pytest -q` | 32 tests; needs `pg_local.sh up` (uses real pgvector DB). Run with `local` providers — no keys. |
| Lint                 | `make lint`                                                                                      | ruff + black (backend), eslint (frontend)                  |
| Frontend build       | `cd frontend && npm run build`                                                                   | Compiles the Next.js app                                   |
| Full-stack verify    | `bash scripts/verify_stack.sh`                                                                   | 6 steps: DB up → migrate → `/health` → CORS → e2e data path (upload→index→search→chat) → frontend build |
| Live smoke           | `bash backend/scripts/e2e_smoke.sh`                                                              | End-to-end against a running stack                         |

CI (`.github/workflows/ci.yml`) runs on push/PR: backend (pgvector service on host
`54329`, ruff/black, `alembic upgrade head`, pytest with `LLM_PROVIDER=local`) and
frontend (`npm ci`, lint, build). **CI needs no Gemini key.**

---

## 7. Admin & authentication

- Single admin from env (`ADMIN_USERNAME`/`ADMIN_PASSWORD`); **no users table**.
- JWT bearer (HS256), OAuth2 *password* flow. `POST /auth/login` takes **form-encoded**
  `username`+`password` → `{access_token, token_type:"bearer"}`. Constant-time
  credential check.
- Protection is **router-level**: `/documents*` and `/admin/*` declare
  `dependencies=[Depends(get_current_admin)]`, plus `GET /auth/me`. Everything else
  (chat, search, health, login) is public.
- Frontend stores the token in `localStorage` and clears it on logout / any `401`.
  `/admin` shows a login form until authenticated.

CLI login:

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=admin123" | jq -r .access_token)
curl -X POST http://127.0.0.1:8000/documents/upload \
  -H "Authorization: Bearer $TOKEN" -F "file=@./HR_Policy.pdf"
```

---

## 8. Milestone status

| #   | Milestone                          | Status                                                          |
| --- | ---------------------------------- | -------------------------------------------------------------- |
| M1  | Foundation (config/db/migrations)  | Done                                                           |
| M2  | Document management                | Done                                                           |
| M3  | Embedding & indexing               | Done                                                           |
| M4  | Semantic search                    | Done                                                           |
| M5  | RAG chat (+ streaming, persistence)| Done                                                           |
| M6  | Admin (stats/indexing/reindex/health) | Done                                                        |
| M7  | Admin-stats fix                    | Done — admin stats/health corrected                            |
| M8  | Deployment + docs                  | Done — `DEPLOYMENT.md`, `docker-compose.yml`, Dockerfiles, CI, this handover |

**Remaining / optional follow-ups** (none blocking): multi-user accounts & RBAC,
hybrid (keyword + vector) search, history-aware retrieval (see §9), feedback system,
document versioning, secrets rotation (see §9), moving the repo under VCS.

---

## 9. Known limitations & gotchas

1. **Retrieval is NOT history-aware.** Only the current question is embedded for the
   vector search (`chat_service._retrieve_and_build_prompt` searches on the cleaned
   question; history is injected into the *prompt* only). So pronoun/ellipsis
   follow-ups ("what about *its* cost?") retrieve poorly — the relevant chunks may not
   be fetched. Workaround: ask self-contained questions; or implement query rewriting
   before retrieval.
2. **PDF extraction uses pypdf `layout` mode** (`extraction.py`), falling back to
   default mode per page on failure. If you change extraction/chunking logic, already
   indexed documents keep their old chunks — you must **re-index** (`POST
   /documents/{id}/reindex` or `POST /admin/reindex`) for changes to take effect.
3. **`gemini-2.5-flash` requires `thinkingBudget=0`** (already set). If you bump to
   another model/version, verify thinking behavior or answers may come back empty.
4. **Local Postgres on `:54329` does not auto-start.** Run `bash
   backend/scripts/pg_local.sh up` before backend/tests; data is under
   `$HOME/.cache/rag-knowledge-base/pg` (off the repo because Windows drvfs can't hold
   Postgres's `0700` perms).
5. **`backend/.env` currently holds real secrets in plaintext** (a live
   `GEMINI_API_KEY`, and a stray `OPENAI_API_KEY`). **Rotate these keys**, keep
   `.env` out of version control (it is gitignored), and move secrets to a manager for
   any deployment. The stray `OPENAI_API_KEY` is unused under the current policy and
   should be removed/rotated.
6. **Changing the embedder breaks retrieval** (see §3): the DB column is `vector(384)`.
   A different embedder/dimension needs a schema migration **and** a full re-index.
7. **`NEXT_PUBLIC_API_BASE_URL` is build-time** in Next 14 — rebuild the frontend to
   change the API URL.
8. **Missing Gemini key silently degrades** to the local extractive answerer (logged
   warning, no error). If chat answers look unusually terse/quote-like, check the key.

---

## 10. Demo / evaluator walkthrough

```bash
# 1. Start everything (Docker path — set GEMINI_API_KEY in .env first)
cp .env.example .env          # edit: GEMINI_API_KEY=..., set admin/JWT/POSTGRES secrets
docker compose up --build
#    (local path instead: make up && make backend && make frontend)

# 2. Health check
curl http://localhost:8000/health        # {"status":"ok"}

# 3. Log in as admin and grab a token
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=admin123" | jq -r .access_token)

# 4. Upload a document (PDF/DOCX/TXT/MD), then confirm it indexed
curl -X POST http://localhost:8000/documents/upload \
  -H "Authorization: Bearer $TOKEN" -F "file=@./HR_Policy.pdf"
curl -s http://localhost:8000/documents -H "Authorization: Bearer $TOKEN" | jq .
#    indexing_status should reach "indexed"

# 5. Public semantic search
curl -s -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query":"How do I request leave?","top_k":5}' | jq .

# 6. Public chat — grounded answer + sources (Gemini)
curl -s -X POST http://localhost:8000/chat/query \
  -H "Content-Type: application/json" \
  -d '{"query":"How do I request leave?"}' | jq .

# 7. Streaming chat (SSE)
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"query":"How do I request leave?"}'
```

**In the browser:** open <http://localhost:3000> for public chat (watch the answer
stream in with source chips). Open <http://localhost:3000/admin>, log in
(`admin`/`admin123` by default), upload a document, watch it reach `indexed` on the
dashboard, then ask about it on the chat page. Note the §9.1 caveat: ask
self-contained questions rather than pronoun follow-ups.
