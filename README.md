# AI-Powered RAG Knowledge Base / Chatbot

This repository contains an AI-powered Retrieval-Augmented Generation (RAG) Knowledge Base and Chatbot system.

The platform has two surfaces:

* **`/` — public chat** (no login). Anyone can ask questions in natural language and
  receive AI-generated answers grounded in the indexed documents, with source references.
  Retrieval uses semantic (vector) search internally.
* **`/admin` — login-gated panel** for administrators to upload, list, delete and
  re-index documents, and to view the indexing/health dashboard.

Document upload and management are **not** on the main page — they live under
`/admin` behind a single-admin JWT login (see [Admin panel & authentication](#-admin-panel--authentication)).

---

# 📂 Project Structure

```bash
rag-knowledge-base/
│
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── database/
│   │   ├── models/
│   │   ├── rag/
│   │   ├── schemas/
│   │   ├── services/
│   │   ├── utils/
│   │   └── vectorstore/
│   │
│   ├── uploads/
│   ├── requirements.txt
│   └── main.py
│
├── frontend/
│   ├── src/
│   ├── components/
│   └── services/
│
├── README.md
├── REQUIREMENTS.md
├── DONE_CRITERIA.md
└── AI_CONTRACT.md
```

---

# 🛠 Technology Stack

## Backend

* FastAPI
* SQLAlchemy 2.0 + Alembic migrations
* **PostgreSQL** relational database (psycopg v3 driver)
* **pgvector** vector store — embeddings live in a `Vector(384)` column with an HNSW cosine ANN index, on the same Postgres
* Pluggable LLM / embedding providers: **local (default, no API keys)**, OpenAI, or Anthropic
* Python 3.10+

> The system runs end-to-end with **no API keys**: the default configuration uses
> a deterministic local embedding + extractive-LLM provider. A self-contained,
> no-sudo userspace **PostgreSQL 16 + pgvector** dev instance is auto-provisioned
> by `backend/scripts/pg_local.sh` (see Getting Started), so you don't need a
> system Postgres install for local development. Set `EMBEDDING_PROVIDER`,
> `LLM_PROVIDER` and the matching API key to switch to OpenAI / Anthropic, and
> point `DATABASE_URL` at any PostgreSQL that has the `pgvector` extension
> available for production.

## Frontend (Optional)

* React
* Next.js
* TypeScript

---

# 📌 Common Database Structure

All tables must include:

```sql
id              BIGINT PRIMARY KEY AUTO_INCREMENT
status          SMALLINT NOT NULL DEFAULT 1
added_time      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
last_modified   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
```

## Status Values

| Value | Meaning             |
| ----- | ------------------- |
| 0     | Inactive            |
| 1     | Active              |
| 5     |  Deleted            |

Soft delete should preferably use:

```sql
status = 0
```

---

# 🚀 Core Features

## 📄 Document Management

* Upload PDF, DOCX, TXT, Markdown
* Store metadata
* Delete and re-index documents

## 🧠 Knowledge Indexing

* Generate embeddings
* Store vectors
* Incremental indexing support

## 💬 AI Chat / RAG

* Natural language querying
* Retrieval-Augmented Generation
* Grounded AI responses
* Source references

## 🔍 Semantic Search

* Similarity-based retrieval
* Top-k matching chunks

## ⚙️ Admin Features (login-gated `/admin`)

* Upload / list / delete / re-index documents
* View indexing status, statistics, and health dashboard
* Protected by a single-admin JWT login (see [Admin panel & authentication](#-admin-panel--authentication))

---

# 📌 System Workflow

## 1. Upload Documents

Upload:

* SOPs
* Policies
* Technical documentation
* Knowledge base files

---

## 2. Document Processing

System performs:

* Text extraction
* Cleaning
* Chunking
* Embedding preparation

---

## 3. Embedding Generation

Embeddings generated using:

* OpenAI embeddings
* Claude-compatible embeddings
* Local models (optional)

---

## 4. Vector Storage

Embeddings are stored in **pgvector** — a `Vector(384)` column on the `chunks`
table in PostgreSQL, with an HNSW `vector_cosine_ops` ANN index for fast
approximate nearest-neighbour retrieval.

---

## 5. User Query Flow

1. User submits question
2. Query embedding generated
3. Relevant chunks retrieved
4. Context prompt built
5. LLM generates answer
6. Sources returned

---

# 📌 Example Response

```json
{
  "answer": "Employees must submit leave requests through...",
  "sources": [
    {
      "document": "HR_Policy.pdf",
      "excerpt": "Employees must submit leave requests..."
    }
  ]
}
```

---

# 🚀 Getting Started

## ✅ Backend Setup

### Prerequisites

Install:

* Python 3.10+
* Virtualenv

No system PostgreSQL install is required for local development: a self-contained,
no-sudo userspace **PostgreSQL 16 + pgvector** instance is provisioned for you by
`scripts/pg_local.sh` (it downloads and extracts Postgres without root and runs
on `127.0.0.1:54329`).

### Setup Steps

```bash
cd backend
python -m venv venv && source venv/bin/activate   # if not already
pip install -r requirements.txt
bash scripts/pg_local.sh up        # provision & start local Postgres + pgvector (no sudo)
alembic upgrade head               # create schema (extension, tables, vector index)
uvicorn app.main:app --reload
```

`alembic upgrade head` applies the managed schema (the `vector` extension, the
`documents` and `chunks` tables, the `chunks.embedding` `Vector(384)` column, and
the HNSW cosine ANN index).

Stop the local Postgres instance when you're done:

```bash
bash scripts/pg_local.sh stop
```

Other `pg_local.sh` subcommands: `status`, `psql`, `url`, `reset`. Its data lives
under `$HOME/.cache/rag-knowledge-base/pg` (kept off the repo because Windows
drvfs can't hold Postgres's `0700` permissions); override the location with the
`RAG_PG_HOME` environment variable.

**Production:** instead of the local dev instance, point `DATABASE_URL` at any
PostgreSQL server that has the `pgvector` extension available, then run
`alembic upgrade head` against it.

Backend runs on:

```bash
http://127.0.0.1:8000
```

Interactive API docs (Swagger UI) are served at `http://127.0.0.1:8000/docs`.

### Running the tests

The suite (32 tests) runs against the `ragdb_test` database, so `pg_local.sh up`
must have been run first:

```bash
cd backend
DATABASE_URL=postgresql+psycopg://raguser@127.0.0.1:54329/ragdb_test ./venv/bin/python -m pytest -q
```

For a live end-to-end smoke check (upload → index → search → chat against a
running stack):

```bash
bash scripts/e2e_smoke.sh
```

---

## ✅ Frontend Setup (Optional)

### Prerequisites

* Node.js 18+
* npm or yarn

### Setup

```bash
cd frontend

npm install

cp .env.local.example .env.local   # optional — sets NEXT_PUBLIC_API_BASE_URL

npm run dev
```

Frontend runs on:

```bash
http://localhost:3000
```

The frontend reads the backend URL from `NEXT_PUBLIC_API_BASE_URL`
(default `http://127.0.0.1:8000`).

> **WSL note (running from a `/mnt/...` Windows drive):** `npm run dev` is very slow
> on the Windows drvfs mount (Next.js recompiles on every navigation, which looks like
> a page that "keeps loading"). For a fast experience on `/mnt`, use **production mode**:
> ```bash
> cd frontend && npm run build && npm run start   # or:  make frontend-prod
> ```
> This compiles once and serves in milliseconds on `http://localhost:3000`. If you ever
> hit a `Cannot find module './XXX.js'` error from `.next`, run `npm run clean` then start again.
> For fast hot-reload dev, keep the project on the Linux filesystem (e.g. `~/…`) instead of `/mnt`.

---

# 🔑 Environment Variables

All settings have working defaults; a `.env` is optional. The defaults below run
the full system locally with **no API keys**, against the auto-provisioned local
PostgreSQL + pgvector instance. See `backend/.env.example` for the complete
annotated list.

```env
# --- Database (PostgreSQL; defaults to the local pg_local.sh instance) ---
DATABASE_URL=postgresql+psycopg://raguser@127.0.0.1:54329/ragdb
# External PostgreSQL example (must have the pgvector extension available):
#   DATABASE_URL=postgresql+psycopg://user:password@db.example.com:5432/rag

# --- Vector store ---
VECTOR_DB=pgvector

# --- Providers: local | openai | anthropic ---
# Selecting openai/anthropic without the matching key gracefully falls back to local.
EMBEDDING_PROVIDER=local
LLM_PROVIDER=local
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# --- RAG / embedding parameters ---
EMBEDDING_DIM=384
CHUNK_SIZE=1000
CHUNK_OVERLAP=200
TOP_K=5

# --- Uploads & CORS ---
MAX_UPLOAD_MB=25
ALLOWED_EXTENSIONS=pdf,docx,txt,md
CORS_ORIGINS=http://localhost:3000

# --- Auth (single admin, JWT) ---
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
JWT_SECRET_KEY=dev-secret-change-me-in-production
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=720
```

> ⚠️ **Production security:** the auth defaults above are **insecure dev values**.
> You **must** set a strong `ADMIN_PASSWORD` and a long, random `JWT_SECRET_KEY`
> before any non-local deployment. Anyone who knows the JWT secret can forge admin
> tokens. Never commit real secrets — `.env` is gitignored; `.env.example` documents
> names only.

The frontend reads only `NEXT_PUBLIC_API_BASE_URL` (in `frontend/.env.local`); auth
vars above are backend-only.

---

# 📌 APIs

## Document APIs (admin JWT required)

```http
POST   /documents/upload
GET    /documents
GET    /documents/{id}
DELETE /documents/{id}
POST   /documents/{id}/reindex
```

## Search API (public)

```http
POST /search
```

## Chat API (public)

```http
POST   /chat/query                    # non-streaming answer (+ persistence)
POST   /chat/stream                   # Server-Sent Events token stream (+ persistence)
GET    /chat/conversations            # list active conversations (newest first)
GET    /chat/conversations/{id}       # one conversation + ordered messages
DELETE /chat/conversations/{id}       # soft-delete a conversation
```

> The chat conversation endpoints are **public** — they are part of the public chat
> experience, not the admin surface.

See [Chat (streaming + history)](#-chat-streaming--history) below for the SSE
event format, conversation persistence, and how to enable hosted-LLM streaming.

---

# 💬 Chat (streaming + history)

The chat is **streaming** and **persistent**: answers arrive token-by-token over
Server-Sent Events, and every conversation is stored in PostgreSQL so it survives
a page refresh or a backend restart.

## Endpoints

| Method & Path                  | Purpose                                                                                  |
| ------------------------------ | ---------------------------------------------------------------------------------------- |
| `POST /chat/query`             | Non-streaming. Returns `{answer, sources, conversation_id, message_id}` and persists the turn. |
| `POST /chat/stream`            | Streams the answer as Server-Sent Events (`text/event-stream`) and persists the turn.    |
| `GET /chat/conversations`      | List active conversations, newest first (`{items, total}`).                              |
| `GET /chat/conversations/{id}` | One conversation plus its messages in chronological order.                               |
| `DELETE /chat/conversations/{id}` | Soft-delete a conversation and its messages (`status = 5`).                           |

### Request body (both `/chat/query` and `/chat/stream`)

```json
{
  "query": "How do I request leave?",
  "conversation_id": 12,
  "top_k": 5,
  "history": [{ "role": "user", "content": "..." }]
}
```

Only `query` is required. Omit `conversation_id` (or send `null`) to start a new
conversation — its id is returned in the response so the next turn can continue it.
`top_k` and `history` are optional; the database is the source of truth for history,
so `history` is only a fallback for the very first turn of a fresh conversation.

## SSE event format (`POST /chat/stream`)

The response is `text/event-stream`. Each frame is a `data:` line:

```text
data: {"delta": "Employees "}
data: {"delta": "must "}
data: {"delta": "submit ..."}
data: {"done": true, "sources": [ ... ], "conversation_id": 12, "message_id": 87}
```

- `{"delta": "..."}` — one chunk of the answer; many of these arrive in order.
- `{"done": true, "sources": [...], "conversation_id": N, "message_id": N}` —
  the terminal frame with the grounding sources and the persisted ids.
- `{"error": "..."}` — emitted instead if generation fails (the stream never 500s).

`curl` example (`-N` disables buffering so frames appear as they stream):

```bash
curl -N -X POST http://127.0.0.1:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"query":"How do I request leave?"}'
```

## Conversation persistence

Conversations and messages are stored in two PostgreSQL tables:

- **`conversations`** — one row per thread (`id`, `title` auto-derived from the
  first user message, plus the common `status`/`added_time`/`last_modified` fields).
- **`messages`** — one row per turn: `conversation_id` (FK → `conversations.id`),
  `role` (`user` | `assistant`), `content`, and `sources` (a JSONB array of
  `{document, excerpt, document_id, score}`; `NULL` for user turns).

Both `/chat/query` and `/chat/stream` persist the user message before generating
and the assistant message (with its sources) after. Listing and reading filter to
active rows; deletion is a **soft delete** (`status = 5`).

## Chat UI

The Next.js chat (`frontend/components/ChatInterface.tsx`) provides:

- streaming answer bubbles that fill in token-by-token, with a typing indicator;
- per-answer source references;
- auto-scroll to the newest content;
- input/button disabled while a response is streaming;
- inline error message with a **Retry** button;
- **New chat** and **Clear** (Clear soft-deletes the conversation server-side);
- loads the most recent persisted conversation automatically on mount.

## Enabling hosted LLM streaming (API keys)

By default `LLM_PROVIDER=local` needs **no API key**: it composes a grounded
answer and streams it by chunking the text server-side. To use a hosted model
with **native token streaming**, edit `backend/.env`:

```env
# OpenAI (model gpt-4o-mini)
OPENAI_API_KEY=sk-...
LLM_PROVIDER=openai

# --- or --- Anthropic (model claude-opus-4-8)
ANTHROPIC_API_KEY=...
LLM_PROVIDER=anthropic

# Optional: OpenAI embeddings (Anthropic has no public embeddings API and
# falls back to the local/OpenAI embedder).
EMBEDDING_PROVIDER=openai
# OPENAI_API_KEY=sk-...   (reuses the key set above)
```

If a selected provider's key is missing, the system **gracefully falls back to
local** rather than failing. **Restart the backend** after editing `backend/.env`.

---

# 🔐 Admin panel & authentication

The app is split into a **public chat** at `/` and a **login-gated admin panel** at
`/admin`. Document upload/list/delete/reindex and the indexing/health dashboard all
live under `/admin` and require a valid admin token; the chat (including conversation
history) and semantic search stay public.

## Authentication model

A **single admin** is configured from environment variables (no users table). Auth is
JWT bearer using the OAuth2 *password* flow:

- **`POST /auth/login`** — public. Takes **form fields** `username` + `password`
  (`application/x-www-form-urlencoded`, FastAPI `OAuth2PasswordRequestForm`) and returns
  `{ "access_token": "<jwt>", "token_type": "bearer" }`. Credentials are checked in
  constant time against `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
- Send **`Authorization: Bearer <token>`** on every protected request.
- **`GET /auth/me`** — protected. Returns the authenticated admin identity
  (`{ "username": "admin" }`).

Tokens are signed with `JWT_SECRET_KEY` (HS256) and expire after
`ACCESS_TOKEN_EXPIRE_MINUTES` minutes (default 720 = 12h). The frontend stores the token
in `localStorage` and clears it on logout or on any `401`.

## Public vs protected route matrix

| Method & Path                       | Access            |
| ----------------------------------- | ----------------- |
| `GET /health`                       | Public            |
| `POST /auth/login`                  | Public            |
| `POST /search`                      | Public            |
| `POST /chat/query`                  | Public            |
| `POST /chat/stream`                 | Public            |
| `GET /chat/conversations`           | Public            |
| `GET /chat/conversations/{id}`      | Public            |
| `DELETE /chat/conversations/{id}`   | Public            |
| `GET /auth/me`                      | **Admin JWT**     |
| `POST /documents/upload`            | **Admin JWT**     |
| `GET /documents`                    | **Admin JWT**     |
| `GET /documents/{id}`               | **Admin JWT**     |
| `DELETE /documents/{id}`            | **Admin JWT**     |
| `POST /documents/{id}/reindex`      | **Admin JWT**     |
| `GET /admin/stats`                  | **Admin JWT**     |
| `GET /admin/indexing-status`        | **Admin JWT**     |
| `POST /admin/reindex`               | **Admin JWT**     |
| `GET /admin/health`                 | **Admin JWT**     |

Protection is enforced at the **router level**: the `/documents` and `/admin` routers
each declare `dependencies=[Depends(get_current_admin)]`, so every route under those
prefixes requires a valid token. An invalid/expired token, or one whose subject is not
the configured admin, returns `401` with a `WWW-Authenticate: Bearer` header.

## Environment variables (backend `.env` / `.env.example`)

| Variable                      | Default                             | Purpose                              |
| ----------------------------- | ----------------------------------- | ------------------------------------ |
| `ADMIN_USERNAME`              | `admin`                             | The single admin's login name.       |
| `ADMIN_PASSWORD`              | `admin123`                          | The single admin's password.         |
| `JWT_SECRET_KEY`              | `dev-secret-change-me-in-production`| HMAC signing key for JWTs.           |
| `JWT_ALGORITHM`               | `HS256`                             | JWT signing algorithm.               |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `720`                               | Token lifetime in minutes (12h).     |

> ⚠️ **CHANGE `ADMIN_PASSWORD` AND `JWT_SECRET_KEY` IN PRODUCTION.** The defaults are
> insecure dev values. Use a strong password and a long, random secret; anyone who learns
> the secret can mint valid admin tokens.

## How to log in

**In the browser:** open `http://localhost:3000` for chat (no login needed). To manage
documents, go to `http://localhost:3000/admin` and sign in with the admin credentials
(`admin` / `admin123` by default). The token is kept in `localStorage`; use **Logout** to
clear it.

**From the CLI** — log in (note: form-encoded, not JSON), then use the token:

```bash
# 1) Log in and capture the bearer token
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=admin123" | jq -r .access_token)

# 2) Use it on a protected request (upload a document)
curl -X POST http://127.0.0.1:8000/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@./HR_Policy.pdf"
```

The same `Authorization: Bearer $TOKEN` header works for every `/documents*` and
`/admin/*` endpoint and for `GET /auth/me`.

---

# 📌 Future Improvements

* Multi-user accounts & RBAC (today: single-admin JWT only)
* Hybrid search
* Feedback system
* Document versioning

---

# 📄 License

This project is proprietary and intended for internal organizational knowledge management systems.
