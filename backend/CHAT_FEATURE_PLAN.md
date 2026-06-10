# Chat Feature Plan — SSE Streaming + Persisted Conversations + Functional UI + e2e

Author: principal engineer (FastAPI / SSE / RAG / Next.js)
Date: 2026-06-03
Scope: **extend** the existing chat code. Do **not** rebuild from scratch. No code is changed by this document.

This plan adds:
1. SSE token streaming (`POST /chat/stream`, `text/event-stream`).
2. PostgreSQL-persisted conversations + messages (new tables, Alembic migration).
3. A clean functional chat UI (bubbles, streaming text + typing indicator, per-answer sources, auto-scroll, loading + inline error/retry, new/clear conversation).
4. Backend pytest + a live e2e shell script.

Local providers (no API keys) keep working; cloud providers stream natively when a key is set.

---

## 0. What exists today (cited, so we build on it)

- **Chat router** — `backend/app/api/chat.py:23-46`: a single `POST /chat/query` (`@router.post("/chat/query", response_model=ChatResponse)`) that calls `chat_service.query(...)` and returns `ChatResponse(answer, sources)`. Router mounted in `backend/app/main.py:63` (`app.include_router(chat.router)`).
- **Chat service** — `backend/app/services/chat_service.py:94-126`: `query(db, question, top_k, history)` = sanitize → `search_service.search` → `build_rag_prompt` → `_generate_answer` (with local fallback at lines 75-91) → `{answer, sources}`. `_build_sources` at lines 48-72 shapes hits into `{document, excerpt, document_id, score}`.
- **LLM providers** — `backend/app/rag/llm.py`: abstract `LLMProvider.generate` (lines 43-48), `LocalExtractiveLLMProvider` (51-148, computes a deterministic extractive answer + appends `(Source: ...)`), `OpenAILLMProvider.generate` (164-187, httpx, non-streaming), `AnthropicLLMProvider.generate` (205-231, httpx, non-streaming), `get_llm_provider()` factory (286-322) with local fallback when no key.
- **Prompt** — `backend/app/rag/prompt.py:63-90`: `build_rag_prompt(query, contexts, history) -> (system, user)`. **Reused as-is** for streaming.
- **Schemas** — `backend/app/schemas/chat.py`: `ChatMessage`, `ChatRequest`, `Source`, `ChatResponse`.
- **Models + mixin** — `backend/app/database/base.py:29-63` `TimestampStatusMixin` (id BIGINT PK autoincrement; status SMALLINT default `Status.ACTIVE=1`; `added_time`/`last_modified` timestamptz default `now()`, `last_modified` has `onupdate=func.now()`). `Status` IntEnum: INACTIVE=0, ACTIVE=1, DELETED=5 (lines 21-26). Model pattern in `backend/app/models/document.py` and `chunk.py`; both registered in `backend/app/models/__init__.py:9-12`.
- **DB session** — `backend/app/database/session.py`: `engine`, `SessionLocal` (`expire_on_commit=False`, line 39), `get_db()` generator dependency (44-50), `init_db()` (53-70).
- **Migration baseline** — `backend/migrations/versions/0001_initial_pg.py` (`revision = "0001_initial_pg"`, `down_revision = None`). Alembic config at `backend/alembic.ini`, env at `backend/migrations/env.py`.
- **Search return shape** — `backend/app/services/search_service.py:98-...` `search(db, query, top_k) -> list[{document_id, document_name, excerpt, chunk_id, score}]` (active-only).
- **Frontend client** — `frontend/services/api.ts`: `chatQuery(query, topK, history)` (160-180) POSTs `/chat/query`; `ChatMessage`/`ChatSource`/`ChatResponse` types (56-71); `handleJson`/`extractError` helpers (78-98); `API_BASE_URL` (7).
- **Frontend UI** — `frontend/components/ChatInterface.tsx` (turns, optimistic user turn, `onAsk`, `onClear`); `frontend/components/Sources.tsx` (per-answer sources list); `frontend/app/page.tsx:1-20` (renders `<ChatInterface/>`); `frontend/app/globals.css:177-242` (existing `.chat-log`, `.turn`, `.turn-user/assistant`, `.sources` styles to extend).
- **Tests** — `backend/tests/conftest.py` runs Alembic against `ragdb_test`, truncates `chunks, documents` per test (line 155), exposes `client`/`db` fixtures and `LEAVE_POLICY_*` helpers. `backend/tests/test_chat.py` covers `/chat/query`. Live e2e at `backend/scripts/e2e_smoke.sh`.

---

## 1. Checklist mapping — PRESENT vs TODO (with file targets)

| Item | Status | Where / What to do |
|---|---|---|
| LLM connected | **PRESENT** | `app/rag/llm.py` (local/OpenAI/Anthropic, factory with fallback). |
| Send/receive APIs | **PRESENT** | `POST /chat/query` in `app/api/chat.py`; FE `chatQuery` in `services/api.ts`. |
| Prompt → response | **PRESENT** | `app/rag/prompt.build_rag_prompt` → `chat_service.query`. |
| Basic chat screen | **PRESENT** | `frontend/components/ChatInterface.tsx` + `Sources.tsx` + `app/page.tsx`. |
| FE/BE comms | **PRESENT** | `services/api.ts` (`API_BASE_URL`, `handleJson`); CORS in `app/main.py:41-47`. |
| Complete chat interface | **TODO (enhance)** | Rebuild `ChatInterface.tsx` in place: bubbles, typing indicator, auto-scroll, retry, new/clear, load-on-mount. |
| Send/receive messages | **PRESENT (extend)** | Keep `/chat/query`; add streaming path + persistence. |
| Streaming | **TODO** | New `POST /chat/stream` (SSE) in `app/api/chat.py`; `generate_stream` in `app/rag/llm.py`; `chat_service.stream_answer`; FE `streamChat()`. |
| Persisted session history | **TODO** | New `app/models/conversation.py` + `message.py`; migration `0002`; persistence in `chat_service`; conversation APIs. |
| Loading / error | **PRESENT (enhance)** | Already in `ChatInterface.tsx`; add inline error + **retry** + disabled-while-streaming. |
| e2e | **PRESENT (extend)** | Add `backend/tests/test_chat_stream.py` + `test_conversations.py`; new `backend/scripts/chat_e2e.sh`. Keep `test_chat.py` + `e2e_smoke.sh`. |

Net: 6 PRESENT, 5 TODO/enhance. Nothing is rebuilt from zero — we add a streaming endpoint and persistence beside the working query path.

---

## 2. Database schema (new tables)

Two new ORM models reusing `TimestampStatusMixin` (so each gets `id BIGINT PK`, `status SMALLINT` 0/1/5, `added_time`/`last_modified` timestamptz).

### `app/models/conversation.py` — `conversations`
- `title: Mapped[str | None] = mapped_column(String(512), nullable=True)` — auto-derived from first user message (truncated) or `NULL` ("New conversation").
- common fields via mixin.

### `app/models/message.py` — `messages`
- `conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=False, index=True)`
- `role: Mapped[str] = mapped_column(String(16), nullable=False)` — `'user'` | `'assistant'` (CHECK enforced in migration).
- `content: Mapped[str] = mapped_column(Text, nullable=False)`
- `sources: Mapped[list | None] = mapped_column(JSONB, nullable=True)` — JSONB array of `{document, excerpt, document_id, score}` (null for user messages).
- common fields via mixin.

Register both in `app/models/__init__.py` (add to imports + `__all__`) so `Base.metadata` and `init_db()` see them.

### Migration: `backend/migrations/versions/0002_conversations_messages.py`
- `revision = "0002_conversations_messages"`, **`down_revision = "0001_initial_pg"`**.
- `upgrade()`:
  - `create_table("conversations", ...)` with the four mixin columns (id BIGSERIAL PK, status SMALLINT `server_default="1"`, added_time/last_modified timestamptz `server_default=now()`) + `title VARCHAR(512) NULL`. Index `ix_conversations_status`.
  - `create_table("messages", ...)` with mixin columns + `conversation_id BIGINT NOT NULL`, `role VARCHAR(16) NOT NULL`, `content TEXT NOT NULL`, `sources JSONB NULL`. `ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name="fk_messages_conversation_id_conversations")`. Indexes: `ix_messages_conversation_id` (lookups), `ix_messages_status`. Optional `CheckConstraint("role IN ('user','assistant')", name="ck_messages_role")`.
- `downgrade()`: drop indexes, drop `messages`, drop `ix_conversations_status`, drop `conversations`. (Do not touch the `vector` extension.)
- Mirror column types/nullability/server-defaults exactly to the ORM (no drift), exactly as `0001` mirrors documents/chunks.

> `JSONB` import: `from sqlalchemy.dialects.postgresql import JSONB` in both the model and the migration.

---

## 3. SSE protocol

### `POST /chat/stream` — `media_type="text/event-stream"`
Request body (extend `ChatRequest`, all back-compat): `{ query: str (min 1), top_k?: int, conversation_id?: int | null, history?: ChatMessage[] }`.
- `conversation_id` optional: continue an existing conversation or create a new one when omitted/null.
- `history` becomes optional/derived — when `conversation_id` is given, the server loads prior messages from the DB as history; client-sent `history` remains accepted for back-compat but DB is the source of truth.

Response is a `StreamingResponse` with **headers**:
- `Cache-Control: no-cache`
- `X-Accel-Buffering: no`  (disable nginx/proxy buffering so deltas flush immediately)
- `Connection: keep-alive`
- (media type already sets `Content-Type: text/event-stream`)

**Event lines** (each terminated by a blank line per SSE framing):
```
data: {"delta": "<token text>"}\n\n      # one per streamed chunk (many)
...
data: {"done": true, "sources": [ {document,excerpt,document_id,score}, ... ], "conversation_id": N, "message_id": M}\n\n
```
- Each `delta` carries the next piece of assistant text (the client concatenates).
- A single terminal event carries `done:true`, the final `sources`, and the resolved `conversation_id` + persisted assistant `message_id`.
- On a mid-stream error: emit `data: {"error": "<message>"}\n\n` then stop (client shows inline error + retry). The endpoint never raises a 500 mid-stream once headers are sent.

### `POST /chat/query` — kept for back-compat (non-streaming)
Unchanged contract `{answer, sources}` **but now also persists** the turn (resolve/create conversation, save user + assistant messages) and accepts optional `conversation_id`, returning it (additive field; existing clients ignore it). `test_chat.py` continues to pass.

---

## 4. Persistence flow per streamed turn

New `chat_service.stream_answer(...)` is a **generator-driving** helper. Order of operations for one turn:

1. **Resolve/create conversation** — if `conversation_id` given and ACTIVE, use it; else `INSERT conversations` (title `NULL`). Commit so the id exists.
2. **Save user message** — `INSERT messages(conversation_id, role='user', content=question, sources=NULL)`; commit. If the conversation `title` is `NULL`, set it from the first user message (truncated ~80 chars) and commit.
3. **Build history** — load prior ACTIVE messages for the conversation ordered by `id ASC` (excluding the just-saved user turn or including it consistently) → list of `{role, content}` for `build_rag_prompt`.
4. **Retrieve + prompt (reuse existing)** — `search_service.search(db, query, top_k)` → `build_rag_prompt(question, hits, history)`; compute `sources = _build_sources(hits)`. If no hits, stream the `_NO_CONTEXT_ANSWER` and persist it with empty sources.
5. **Stream deltas** — call `provider.generate_stream(user, system=system)` (provider selected via `get_llm_provider()`), **accumulate** the full text while `yield`-ing each delta as an SSE `data: {"delta": ...}` line. Apply the same provider-failure fallback as `_generate_answer` (lines 75-91): on `RateLimitError`/`ProviderError`/unexpected, fall back to `LocalExtractiveLLMProvider().generate_stream(...)`.
6. **Save assistant message** — after the stream completes, `INSERT messages(conversation_id, role='assistant', content=<accumulated>, sources=<JSONB>)`; commit. Capture `message_id`.
7. **Emit terminal event** — `data: {"done": true, "sources": [...], "conversation_id": N, "message_id": M}`.

### ⚠️ Design note — DB session lifecycle under `StreamingResponse`
With FastAPI `StreamingResponse`, the body generator runs **after** the route function returns, by which point a `Depends(get_db)` session is (or may be) already closed in the `finally:` of `get_db()` (`session.py:44-50`). Persisting in steps 1-2-6 against that session would hit "session is closed" / detached-instance errors.

**Resolution:** the streaming generator **opens and owns its own session** via `SessionLocal()` (do **not** rely on the request-scoped `Depends(get_db)` session inside the generator). Pattern:

```python
def event_stream():
    db = SessionLocal()
    try:
        conv = resolve_or_create(db, ...)        # steps 1-2 (commit)
        hits = search_service.search(db, ...)     # step 4
        system, user = build_rag_prompt(...)
        full = []
        for delta in provider_stream_with_fallback(user, system):
            full.append(delta)
            yield sse(delta=delta)               # step 5
        msg = save_assistant(db, conv.id, "".join(full), sources)  # step 6 (commit)
        yield sse(done=True, sources=sources, conversation_id=conv.id, message_id=msg.id)
    except Exception as exc:
        yield sse(error=str(exc))                # never 500 mid-stream
    finally:
        db.close()
return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)
```

The non-streaming `/chat/query` path may keep using the `Depends(get_db)` session (it completes before returning), but for symmetry/safety it can route through the same service that opens its own session for persistence.

---

## 5. Provider streaming (`app/rag/llm.py`)

Add an **optional** streaming method to the abstraction; keep every existing `generate()` intact.

- `LLMProvider`: add `def generate_stream(self, prompt, system=None) -> Iterator[str]` with a **default implementation** that yields `self.generate(prompt, system)` once (so any provider streams "in one chunk" if it doesn't override). Keeps the ABC non-breaking.
- `LocalExtractiveLLMProvider.generate_stream`: compute the extractive answer via existing `generate(...)` logic, then **chunk it word-by-word** (yield `word + " "`), preserving the trailing `(Source: ...)` line. Deterministic, no network — satisfies "local providers must work" and gives the FE a real multi-delta stream.
- `OpenAILLMProvider.generate_stream`: native streaming — POST with `"stream": true` to `/v1/chat/completions` using `httpx.stream(...)`, parse `data: ` SSE lines, extract `choices[0].delta.content`, yield each. On any transport/parse error or rate limit, **fall back** to `LocalExtractiveLLMProvider().generate_stream(...)` (mirror the existing `_post_with_retry`/error mapping; reuse 429 → `RateLimitError`).
- `AnthropicLLMProvider.generate_stream`: native streaming — POST with `"stream": true` to `/v1/messages`, parse `content_block_delta` events, yield `delta.text`. Same local fallback on error.
- Shared helper for SSE-over-httpx line parsing (cloud providers) to avoid duplication.

The service-level fallback (step 5) remains as a second safety net even though providers self-fallback.

---

## 6. Conversation APIs (`app/api/chat.py` + `app/services/chat_service.py` + `app/schemas/chat.py`)

New Pydantic schemas: `ConversationOut {id, title, status, added_time, last_modified}`, `MessageOut {id, role, content, sources: list[Source] | null, added_time}`, `ConversationDetail {…ConversationOut, messages: list[MessageOut]}`, `ConversationList {items, total}`.

Endpoints:
- `GET /chat/conversations` → ACTIVE conversations (status == `Status.ACTIVE`), **newest first** (`ORDER BY last_modified DESC, id DESC`). Returns `ConversationList`.
- `GET /chat/conversations/{id}` → one ACTIVE conversation with its ACTIVE messages **ordered `id ASC`** (chronological). 404 if missing/inactive.
- `DELETE /chat/conversations/{id}` → **soft delete**: set `status = Status.DELETED` (5) on the conversation (and optionally its messages); returns `{detail}` like documents delete. Already-deleted/absent → 404.

Service functions: `list_conversations(db)`, `get_conversation(db, id)`, `soft_delete_conversation(db, id)`, plus the persistence helpers from §4.

---

## 7. Frontend

### `frontend/services/api.ts`
- New types: `Conversation {id, title, status, added_time, last_modified}`, `Message {id, role, content, sources: ChatSource[] | null, added_time}`, `ConversationDetail`, `ConversationList`.
- `streamChat(params, handlers)` — SSE via **`fetch` + `response.body.getReader()` + `TextDecoder`**:
  - POST `/chat/stream` with JSON body `{query, top_k?, conversation_id?}`; header `Accept: text/event-stream`.
  - Read the byte stream, decode incrementally, **buffer and split on `\n\n`**, for each `data: ` line `JSON.parse` the payload:
    - `delta` → `handlers.onDelta(text)`
    - `done` → `handlers.onDone({sources, conversation_id, message_id})`
    - `error` → `handlers.onError(message)`
  - Handle partial frames across reads (keep a residual buffer). Surface non-OK initial responses through `extractError` before reading the stream.
- `getConversations()`, `getConversation(id)`, `deleteConversation(id)` using existing `handleJson`/`extractError`.
- Keep `chatQuery(...)` for back-compat.

### `frontend/components/ChatInterface.tsx` (rebuilt in place)
- State: `messages: Message[]`, `conversationId: number | null`, `input`, `isStreaming`, `streamingText`, `error`, `lastSentQuery` (for retry).
- **On mount**: `getConversations()` → if any, `getConversation(latest.id)` → hydrate `messages` + `conversationId` (load latest persisted conversation).
- **Send**: optimistically append user bubble; clear input; set `isStreaming`; append a placeholder assistant bubble fed by `streamingText`. Call `streamChat`:
  - `onDelta` → append to `streamingText` (assistant bubble grows live).
  - `onDone` → finalize assistant message with full text + `sources`; set `conversationId`; clear `streamingText`/`isStreaming`.
  - `onError` → set inline `error`, keep `lastSentQuery`, stop streaming.
- **Typing indicator**: animated dots in the assistant bubble while `isStreaming` and `streamingText` is empty.
- **Per-message sources**: reuse `Sources.tsx` under each assistant bubble.
- **Auto-scroll**: `ref` to the message list bottom; `useEffect` scrolls into view on `messages`/`streamingText` change.
- **Input**: textarea — **Enter sends, Shift+Enter newline**; disabled while `isStreaming`; send button disabled when empty/streaming.
- **Inline error + Retry**: error banner with a "Retry" button that re-sends `lastSentQuery` to the same `conversationId`.
- **New conversation**: button → clear `messages`/`conversationId`/`error` (next send creates a fresh server conversation).
- **Clear conversation**: button → `deleteConversation(conversationId)` (soft delete) then reset to a new conversation.

### `frontend/app/globals.css`
Extend the existing chat block (currently `.chat-log`/`.turn*`/`.sources`, lines 177-242) with plain CSS:
- bubble alignment (user right / assistant left), max-width, rounded corners, colors from the existing `--primary`/`--card`/`--border` vars;
- `.typing-indicator` with a 3-dot keyframe animation;
- scrollable message area with fixed max-height + `overflow-y:auto`;
- error banner + retry button styling (reuse `.error`/`button.secondary`);
- conversation toolbar (New / Clear) styling.
No CSS framework; plain CSS only.

---

## 8. Tests / e2e

### Backend pytest (local providers + `ragdb_test`)
Reuse `conftest.py` fixtures. **Add `conversations, messages` to the per-test TRUNCATE** at `conftest.py:155` (`TRUNCATE chunks, documents, conversations, messages RESTART IDENTITY CASCADE`) so new tables reset cleanly; the session fixture already runs `alembic upgrade head` which will include `0002`.

- `backend/tests/test_chat_stream.py`:
  - **Multi-delta stream**: POST `/chat/stream` (after uploading `LEAVE_POLICY_TEXT`); parse the `text/event-stream` body, assert **≥2 `delta` events**, reassembled answer contains `"HR portal"` or `"approval"`, and a terminal `done:true` event with non-empty `sources`, a `conversation_id`, and a `message_id`.
  - **Persistence**: after streaming, assert a `conversations` row exists and **2 `messages`** (user + assistant) with correct roles; assistant `sources` is non-null JSONB.
  - **Continue conversation**: second `/chat/stream` with the returned `conversation_id` → same conversation now has **4 messages** in `id ASC` order.
  - **No-context**: stream with empty KB → graceful "not found" answer, empty sources, still persisted (never 500).
  - TestClient SSE note: use `client.stream("POST", "/chat/stream", json=...)` (httpx streaming) or read `resp.text` and split on `\n\n`; assert on parsed `data:` payloads.
- `backend/tests/test_conversations.py`:
  - `GET /chat/conversations` newest-first; `GET /chat/conversations/{id}` returns ordered messages; `DELETE` soft-deletes (status→5) and the conversation then 404s / drops from the list.
- `backend/tests/test_chat.py`: **unchanged + extended** — assert `/chat/query` still returns `{answer, sources}` **and** now persists a conversation + 2 messages.

### Live e2e — `backend/scripts/chat_e2e.sh`
Model it on `e2e_smoke.sh` (set `DATABASE_URL`=`ragdb`, `LLM_PROVIDER=local`; `pg_local.sh up`; `alembic upgrade head`; start uvicorn; poll `/health`; upload + wait for `indexed`). Then:
- **Streaming check**: `curl -N -s` `POST /chat/stream` with a leave question; assert the raw stream contains **≥2 `data: {"delta"` lines** (multi-token) and exactly one `"done": true` line with `conversation_id`.
- **Persisted conversation**: extract `conversation_id`; `GET /chat/conversations/{id}` → assert **≥2 messages**; send a follow-up `/chat/stream` with the same `conversation_id`; re-GET → assert **≥4 messages**.
- `set -euo pipefail` + pass/fail counters + uvicorn-log tail on failure, matching the existing script's conventions. Keep `e2e_smoke.sh` as-is.

---

## 9. Risks & mitigations

- **Streaming + DB session lifecycle** (highest): `Depends(get_db)` closes before/while the `StreamingResponse` generator runs → persistence fails. **Mitigation:** open a dedicated `SessionLocal()` inside the generator and `close()` it in `finally` (see §4). `expire_on_commit=False` (already set) lets us read ids after commit.
- **SSE buffering**: proxies/dev servers may buffer and break live deltas. **Mitigation:** `Cache-Control: no-cache` + `X-Accel-Buffering: no`; flush per event (blank-line-terminated frames); local provider chunks word-by-word so even local mode produces a visible stream.
- **TestClient SSE consumption**: Starlette `TestClient` returns the full body; reading `resp.text` is fine, but to assert *multiple* deltas use `client.stream(...)` (httpx) or split the collected body on `\n\n`. Plan tests around parsed `data:` payloads, not timing.
- **JSONB sources**: ensure import `from sqlalchemy.dialects.postgresql import JSONB` in both model and migration; serialize `sources` as a plain list of dicts (the `_build_sources` shape) so round-trips are stable. Nullable for user messages.
- **Embedding / active filters unchanged**: retrieval still goes through `search_service.search` (active-only); no change to `EMBEDDING_DIM` (384), the pgvector `Vector` column, or the HNSW index — `0002` only adds conversation/message tables.
- **Back-compat**: `/chat/query` contract (`{answer, sources}`) preserved; `conversation_id` is additive; existing `test_chat.py` and `e2e_smoke.sh` keep passing. FE `chatQuery` retained.
- **Provider key absence**: cloud `generate_stream` self-falls-back to local; service-level fallback is the second net — no 500s when keys are missing.

---

## 10. File-change summary (for the implementation phase — not done here)

**New**
- `backend/app/models/conversation.py`, `backend/app/models/message.py`
- `backend/migrations/versions/0002_conversations_messages.py`
- `backend/tests/test_chat_stream.py`, `backend/tests/test_conversations.py`
- `backend/scripts/chat_e2e.sh`

**Modified**
- `backend/app/models/__init__.py` (register new models)
- `backend/app/api/chat.py` (`/chat/stream`, conversation endpoints, persist in `/chat/query`)
- `backend/app/services/chat_service.py` (`stream_answer`, persistence + conversation helpers)
- `backend/app/rag/llm.py` (`generate_stream` on base + 3 providers)
- `backend/app/schemas/chat.py` (`conversation_id` on request; conversation/message schemas)
- `backend/tests/conftest.py` (TRUNCATE new tables)
- `backend/tests/test_chat.py` (assert persistence)
- `frontend/services/api.ts` (`streamChat`, conversation client fns + types)
- `frontend/components/ChatInterface.tsx` (rebuilt in place)
- `frontend/app/globals.css` (chat bubble/typing/toolbar styles)
