# Multi-Tenant Data Isolation Plan — RAG Chatbot

Status: DESIGN (read-only analysis; no code changed). Target: convert the
single-admin app into a multi-tenant app where every authenticated user only
ever sees their own documents and conversations, while admin-uploaded "global"
documents silently ground every user's answers without appearing as their
sources.

This plan is grounded in the current code:
- No `User` model. Auth is a single hardcoded admin (`settings.ADMIN_USERNAME` /
  `ADMIN_PASSWORD`); `core/security.py:authenticate_admin` does a constant-time
  compare, `create_access_token` sets `sub=<username>`, and `get_current_admin`
  asserts `sub == settings.ADMIN_USERNAME`.
- `Document` has `uploaded_by: str|None` (default `"admin"`, anon uploads use
  `"anonymous"`) but no `owner_id` / `is_global`.
- `Conversation` has only `title` + the timestamp/status mixin — no `user_id`.
- `Chunk` carries the embedding (`Vector(384)`) and a FK to `documents.id`.
- Retrieval: `search_service.search(db, query, top_k)` → embeds query (local
  384-dim hashing embedder) → `PgVectorStore.query(embedding, top_k)` (cosine
  `<=>`, filters only `Chunk.status==ACTIVE AND Document.status==ACTIVE`) plus a
  `keyword_query` ILIKE rescue, then a lexical re-rank blend. **There is no
  ownership filter anywhere** — every user's chunks are candidates for every
  query today.
- `chat_service.query` / `stream_answer` → `_retrieve_and_build_prompt` →
  `search_service.search` → `build_rag_prompt` → LLM → `_build_sources`.
- Chat history is global: `list_conversations` / `get_conversation_or_404` /
  `soft_delete_conversation` filter only on `status==ACTIVE`, and the frontend
  loads "the most recent persisted conversation" for everyone.
- Migrations: linear Alembic chain `0001 → 0002 → 0003`, run on Render boot via
  `entrypoint.sh` (`alembic upgrade head`). Style: explicit
  `op.create_table` / `op.add_column` with `server_default`, hardcoded constants,
  matching the ORM exactly. `migrations/env.py` imports `app.models` so
  `Base.metadata` is complete.
- Sources currently render a numeric `score` (`Sources.tsx`,
  `src.score.toFixed(3)`); answers are plain text (no markdown rendering).

## Fixed decisions (design to these)
- Registration identity = **email** (open self-registration, email + password).
- Existing documents = **admin global**: backfill all existing docs to
  `is_global=true`, `owner_id=null`.
- Rendering = react-markdown + KaTeX; build everything then deploy once.

---

## 1. Data model

### New table: `users`
```
User (table "users", Base + TimestampStatusMixin)
  id             BIGINT PK (from mixin)
  status         SMALLINT  (from mixin; ACTIVE=1, lets us soft-disable too)
  added_time     timestamptz (from mixin)  -> serves as created_at
  last_modified  timestamptz (from mixin)
  email          CITEXT UNIQUE NOT NULL    -- case-insensitive unique identity
  password_hash  String(255) NOT NULL
  role           String(16) NOT NULL default 'user' server_default 'user'  (user|admin)
  is_active      Boolean NOT NULL default true server_default true
```
Notes:
- Use Postgres `CITEXT` for `email` so `Foo@x.com` and `foo@x.com` collide on the
  UNIQUE index. `CREATE EXTENSION IF NOT EXISTS citext` in the migration (same
  pattern as `0001`'s `CREATE EXTENSION vector`). If avoiding the extension is
  preferred, store `email` lowercased in a `String(320)` column with a UNIQUE
  index and lowercase on write — but CITEXT is cleaner and recommended here.
- `created_at` requirement is satisfied by the mixin's `added_time`; do not add a
  duplicate column.
- `is_active` is distinct from the mixin `status`: `is_active=false` is an admin
  disable toggle; `status=DELETED` is a soft delete. `get_current_user` rejects
  both.
- A CHECK constraint `role IN ('user','admin')` mirrors the messages-role check
  in `0002`.

### `documents` — add two columns (keep all existing)
```
owner_id   BIGINT NULL  FK -> users.id  (indexed)   -- null == not user-owned
is_global  Boolean NOT NULL default false server_default false
```
- `owner_id` nullable: global/admin docs and legacy backfilled docs have
  `owner_id = NULL, is_global = true`. A user's own upload sets
  `owner_id = <user id>, is_global = false`.
- Keep `uploaded_by` (free-text audit string) untouched.
- Index `owner_id` (the isolation filter joins/filters on it). Add
  `ix_documents_owner_id`.

### `conversations` — add one column (keep all existing)
```
user_id  BIGINT NOT NULL  FK -> users.id  (indexed)
```
- Backfill: existing conversations are pre-multi-tenant global history. Assign
  them to the seeded admin user (`user_id = <admin id>`) so the column can be
  `NOT NULL` after backfill. Add `ix_conversations_user_id`.

No change to `messages` or `chunks` schemas. Chunk ownership is derived through
`chunks.document_id → documents.owner_id/is_global` (a JOIN), so chunks never
need a redundant owner column.

---

## 2. Auth

### Password hashing
- Add `passlib[bcrypt]` (recommended) — `passlib.context.CryptContext(schemes=["bcrypt"])`
  for `hash()` / `verify()`. Alternative: the `bcrypt` lib directly. (`PyJWT`
  already provides JWT; keep it.)

### `core/security.py` refactor
- Keep `create_access_token` / `decode_token` but extend the payload:
  `{"sub": str(user_id), "role": role, "exp": ...}`. `sub` becomes the **user
  id** (string), not the username — this is the central change.
- Add `hash_password(pw) -> str` and `verify_password(pw, hash) -> bool`.
- New dependency `get_current_user(token, db) -> User`:
  1. decode token (401 on expired/invalid, reuse `_CREDENTIALS_EXCEPTION`),
  2. load `User` by `int(sub)`,
  3. 401 if missing, `status != ACTIVE`, or `is_active == false`.
  Because it needs the DB, it takes `db: Session = Depends(get_db)`.
- New dependency `require_admin(user = Depends(get_current_user)) -> User`:
  403 if `user.role != "admin"`.
- Refactor `get_current_admin` to be an alias of `require_admin` so existing
  admin/document routes keep working without rewrites:
  `dependencies=[Depends(get_current_admin)]` on `admin_router` /
  `admin.router` now resolves to "must be an admin user". Drop the old
  `sub == ADMIN_USERNAME` check and `authenticate_admin`.

### `api/auth.py`
- `POST /auth/register` (public, gated by `settings.ALLOW_REGISTRATION`):
  body `{email, password}` (pydantic `EmailStr`). Lowercase/normalize email,
  reject duplicates (409), hash password, create `User(role="user",
  is_active=true)`, return a token (auto-login) or 201.
- `POST /auth/login`: keep `OAuth2PasswordRequestForm`; treat `form.username` as
  the **email**. Look up the user by email, `verify_password`, check
  `is_active`/status, then `create_access_token(subject=str(user.id), role=user.role)`.
  The seeded admin logs in here too (admin is just a `User` with `role="admin"`).
- `GET /auth/me`: `Depends(get_current_user)`, return
  `{id, email, role}` (never `password_hash`).

### Admin seeding
- The migration seeds one admin `User` from `settings.ADMIN_USERNAME` +
  `ADMIN_PASSWORD`. `ADMIN_USERNAME` ("admin") is not a valid email, so derive a
  login email: if `ADMIN_USERNAME` contains `@`, use it verbatim; otherwise use
  `admin@local` (documented) — or better, read a new optional
  `ADMIN_EMAIL` setting (default `admin@local`) and seed that. Password is
  bcrypt-hashed **inside the migration** (import `passlib` there; migrations may
  import app code, and `env.py` already imports `app.*`). `role="admin"`,
  `is_active=true`.

---

## 3. Tenant isolation (CRITICAL)

**Rule:** for any retrieval issued by `current_user`, the candidate chunk set is
restricted to chunks whose document satisfies
`Document.owner_id == current_user.id OR Document.is_global == true`
(in addition to the existing `Chunk.status==ACTIVE AND Document.status==ACTIVE`).
No other user's chunk may ever enter candidates → re-rank → prompt → answer →
sources.

### Concrete mechanism (recommended): filter in the SQL JOIN
`PgVectorStore.query` and `keyword_query` already `JOIN Document ON
Document.id == Chunk.document_id`. Thread an ownership scope **into that JOIN's
WHERE clause** so isolation is enforced in the database, not in Python (cannot be
bypassed, and never wastes top_k slots on foreign chunks).

Add a parameter to the `VectorStore` interface (`base.py`) and `PgVectorStore`:
```
def query(self, embedding, top_k, *, owner_id: int | None = None,
          include_global: bool = True) -> list[QueryResult]
def keyword_query(self, embedding, terms, top_k, *, owner_id=None,
                  include_global=True) -> list[QueryResult]
```
WHERE addition (both methods):
```
or_(
    Document.owner_id == owner_id,            # the caller's own docs
    Document.is_global.is_(True),             # admin-global docs (if include_global)
)
```
- `owner_id` is required for normal user queries. When `owner_id is None` and
  `include_global` is True, restrict to `is_global == true` only (an
  unauthenticated/global-only retrieval). **Never** call with a wildcard that
  returns all owners.
- Default-deny is the safe posture: a missing scope must NOT silently fall back
  to "all documents". (See Risks.)

Why SQL-level over a Python `allowed_document_ids` set: the IDF-free embedder
already needs a wide over-fetch (`_OVERFETCH_MIN_CANDIDATES = 50`) plus the
keyword ILIKE rescue. Filtering after the fact would let foreign chunks consume
the candidate budget and could starve the user's own relevant chunks. Filtering
in the `<=>` query keeps the candidate pool entirely in-tenant.

### Functions that must thread `current_user`
- `chat.py:chat_query` / `chat_stream` (and `search.py`) gain
  `current_user = Depends(get_current_user)` and pass `current_user.id` down.
- `chat_service.query(...)` and `chat_service.stream_answer(...)` gain an
  `owner_id: int` parameter.
- `chat_service._retrieve_and_build_prompt(db, question, top_k, history, owner_id)`
  passes `owner_id` to `search_service.search`.
- `search_service.search(db, query, top_k, *, owner_id, include_global=True)`
  passes `owner_id`/`include_global` to both `store.query` and
  `store.keyword_query`.
- `stream_answer` opens its own `SessionLocal()` and is NOT a FastAPI dependency,
  so the route must capture `current_user.id` (a plain int) and pass it into
  `stream_answer(..., owner_id=...)` — do not pass the ORM `User` across the
  session boundary.

`indexing_service` is unaffected (indexing is per-document and writes the chunk
rows of the doc being indexed; ownership lives on the document row).

---

## 4. Source-reference rules

Displayed sources for a user = **only the user's own documents**:
`owner_id == current_user.id AND is_global == false`, ordered by relevance
(descending — the order retrieval already returns), with **no numeric score**.
Admin-global docs (`is_global == true`) are allowed to ground the answer (they
are in the candidate pool and prompt context) but are **excluded from the
sources list**. If the answer was grounded only on global docs, the user sees
**no sources**.

### Enforcement point
In `chat_service._build_sources(hits, owner_id)`:
- The retrieval hits already carry `document_id`. To know per-hit
  `owner_id`/`is_global`, have `search_service.search` include `owner_id` and
  `is_global` in each result dict (it already resolves the `Document` row in
  `_active_documents_by_id`; add those two fields to the returned dict).
- `_build_sources` keeps a hit **iff** `hit["owner_id"] == owner_id and not
  hit["is_global"]`. Global-doc hits are dropped from the source list.
- Drop the `score` key from the emitted source dicts.

### API / schema change
- `schemas/chat.py:Source` — remove the required `score` field (or make it
  optional and stop populating it). `ChatResponse.sources` then never carries a
  score.
- The SSE `done` frame's `sources` is filtered the same way (it reuses
  `_build_sources`).

### Frontend (`Sources.tsx`)
- Remove `<span className="source-link-score">{src.score.toFixed(3)}</span>`.
- Keep document name + optional `· p.N` and the `documentFileUrl` deep link.
- `SearchResults.tsx` also renders `r.score.toFixed(3)`; if `/search` stays
  user-scoped, apply the same source rules and drop the score there too.

Existing safeguards stay: `_MIN_RELEVANCE_SCORE` thresholding,
`_is_no_answer` → drop sources, small-talk → no sources.

---

## 5. Chat scoping

- `Conversation.user_id` (NOT NULL FK, see §1) ties every conversation to its
  creator.
- All conversation reads/writes scope by `user_id`:
  - `chat_service.resolve_or_create_conversation(db, conversation_id, question,
    owner_id)` — when creating, set `user_id = owner_id`; when resolving an
    existing id, additionally require `Conversation.user_id == owner_id` (else
    treat as not-found and create a new one — never attach to another user's
    thread).
  - `list_conversations(db, user_id)` — `WHERE status==ACTIVE AND
    user_id==<user_id>`.
  - `get_conversation_or_404(db, conversation_id, user_id)` and
    `get_conversation_messages` — add `user_id` filter; a foreign id 404s
    (no leak of existence).
  - `soft_delete_conversation(db, conversation_id, user_id)` — scoped delete.
- `chat.py` routes add `Depends(get_current_user)` and pass `current_user.id`.
- Auto-new conversation per login/session: the frontend stops auto-loading "the
  most recent conversation for everyone" (`app/page.tsx` mount effect). On a
  fresh login it starts a new chat (`activeId = null`); history list is the
  user's own conversations only. A new conversation id is returned in the
  `done`/`ChatResponse` payload exactly as today.
- `messages` inherit scoping transitively (always fetched via a `user_id`-scoped
  conversation).

---

## 6. Existing-data migration (single Alembic revision `0004_multitenant`)

`down_revision = "0003_chunk_page"`, matching the existing chain; reversible;
idempotent (guard creates with IF NOT EXISTS where Alembic allows, and use
checks for re-run safety). Runs on Render boot via `entrypoint.sh` (`alembic
upgrade head`) — no new step needed.

`upgrade()` order:
1. `op.execute("CREATE EXTENSION IF NOT EXISTS citext")`.
2. `op.create_table("users", ...)` with mixin columns (id BIGSERIAL PK, status
   SMALLINT server_default '1', added_time/last_modified timestamptz
   server_default now()), `email CITEXT`, `password_hash`, `role` server_default
   'user', `is_active` server_default true; UNIQUE index on `email`
   (`ix_users_email`), CHECK `role IN ('user','admin')`.
3. Seed admin: `op.bulk_insert` (or `op.execute(text(...))`) one row — email from
   `ADMIN_EMAIL`/derived, `password_hash = passlib.hash(ADMIN_PASSWORD)`,
   `role='admin'`, `is_active=true`. Capture its id (use a fixed email and a
   follow-up `SELECT id` via a bound connection, or insert with
   `RETURNING id`).
4. `documents`: `op.add_column owner_id BIGINT NULL`, `is_global BOOLEAN NOT NULL
   server_default false`; FK `fk_documents_owner_id_users`; `ix_documents_owner_id`.
5. Backfill documents: `UPDATE documents SET is_global = true, owner_id = NULL`
   (every pre-existing doc becomes admin-global, per fixed decision).
6. `conversations`: `op.add_column user_id BIGINT` **nullable first**, then
   `UPDATE conversations SET user_id = <admin id>` (backfill to the seeded
   admin), then `op.alter_column ... nullable=False`; FK
   `fk_conversations_user_id_users`; `ix_conversations_user_id`.

`downgrade()` (reverse order): drop indexes/FKs, drop `conversations.user_id`,
drop `documents.is_global`/`owner_id`, drop `users`. Leave `citext` extension in
place (mirrors `0001` not dropping `vector`).

Hardcode the admin email/derivation logic in the migration with a clear comment
(migrations are immutable point-in-time artifacts; importing live
`settings.ADMIN_PASSWORD` at run time is fine since `env.py` already imports
`app.*`).

---

## 7. New dependencies & config

### Backend (`requirements.txt`)
- `passlib[bcrypt]` (password hashing) — or `bcrypt` directly.
- `email-validator` (enables pydantic `EmailStr`; equivalently depend on
  `pydantic[email]`).

### Frontend (`package.json`)
- `react-markdown`, `remark-gfm`, `remark-math`, `rehype-katex`, `katex`
  (render assistant answers as markdown + LaTeX in `ChatInterface.tsx`'s
  `bubble-content`; import `katex/dist/katex.min.css`).

### Config (`core/config.py`)
- `ALLOW_REGISTRATION: bool = True` — gate `POST /auth/register`.
- `ADMIN_EMAIL: str = "admin@local"` — admin login email for the seeded user
  (used by the migration seed; keep `ADMIN_USERNAME`/`ADMIN_PASSWORD` for the
  seed too).
- Existing `JWT_SECRET_KEY` etc. unchanged.

---

## 8. Risks / gotchas

- **Default-deny isolation.** The new `owner_id` scope parameter must have NO
  "all documents" fallback. If a route forgets to pass `current_user`, the query
  must return nothing (or 401), never every tenant's chunks. Make `owner_id`
  required on the user-facing path and add a test asserting cross-tenant queries
  return zero foreign chunks.
- **Enforce server-side, not in the UI.** Dropping the score / filtering sources
  in `Sources.tsx` is cosmetic; the authoritative filter is the SQL ownership
  clause in `PgVectorStore` and `_build_sources`. Never rely on the frontend to
  hide foreign data.
- **Local hash embedder semantic weakness** is already mitigated by the keyword
  ILIKE rescue + lexical blend; the new ownership WHERE clause must be added to
  BOTH `query` and `keyword_query`, or the rescue path would leak foreign chunks.
- **Mixed-origin grounding.** An answer can be grounded by a blend of the user's
  own docs and admin-global docs. Sources show only the user's own; if only
  global docs contributed, show none. Verify `_build_sources` filtering does not
  also strip the prompt context (context vs. sources are separate concerns —
  global docs stay in `build_rag_prompt` input but leave `_build_sources`).
- **No metadata leakage in schemas.** `/auth/me`, conversation, and document
  responses must never expose other users' `email`, `owner_id`, or
  `password_hash`. Keep `password_hash` out of every Pydantic `…Out` schema.
  `DocumentOut` currently exposes `uploaded_by`; ensure list/get document routes
  are user-scoped so a user can't enumerate others' filenames.
- **Public `/documents/{id}/file`** is currently unauthenticated (deep-linkable).
  After isolation, a raw file URL could expose another user's private document.
  Decide: either gate `/file` behind `get_current_user` + ownership/global check,
  or accept that file URLs are unguessable-but-public. Recommended: scope it
  (own-or-global) to avoid an isolation hole. (Anonymous public upload at
  `/documents/upload` also needs an owner now — either require auth for upload or
  attribute anon uploads to a NULL owner that is never user-visible.)
- **`conversations.user_id` NOT NULL** requires the backfill UPDATE to run
  BEFORE the `alter_column nullable=False`; ordering in the single migration is
  load-bearing.
- **CITEXT requires the extension**; ensure the migration creates it before the
  `users` table (same ordering constraint as `vector` in `0001`).
- **JWT `sub` semantics change** from username to user id — any cached/issued
  admin tokens are invalidated on deploy; acceptable for a one-shot cutover.
- **`stream_answer` session boundary**: pass `owner_id` as an int, never the ORM
  `User`, since the streaming generator owns a separate `SessionLocal()`.
