# Employee Zone Integration — Current State of the RAG Knowledge Base API

Scope: factual audit of the existing FastAPI "RAG Knowledge Base API" before integrating it
into the Employee Zone mobile app. Grounded entirely in the current source. **Read-only** — no
application code was modified.

Fixed integration target (for reference, NOT yet implemented):
- Auth = SSO / trusted token from Employee Zone (no second login).
- KB = shared company knowledge base; employees query admin-uploaded global docs only and cannot upload.
- Client = native mobile app calling the RAG API directly.
- Deliverable = plan/spec only.

---

## 1. AUTH — how identity works today

Email + password, self-issued JWT. The Employee Zone SSO model is **not** present.

- **JWT algorithm + secret.** HS256, symmetric, signed with a single shared secret.
  - `JWT_ALGORITHM: str = "HS256"` and `JWT_SECRET_KEY: str = "dev-secret-change-me-in-production"` — `backend/app/core/config.py:98-99`.
  - Encoding/decoding both read these at call time: `backend/app/core/security.py:76` (`jwt.encode(..., algorithm=settings.JWT_ALGORITHM)`), `backend/app/core/security.py:82` (`jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])`).
- **Token claims.** `{"sub": str(user_id), "role": role, "exp": expire}` — `backend/app/core/security.py:75`. `sub` is the **user id** (string), not the email/username; `role` is `"user"` or `"admin"`.
- **Expiry.** `ACCESS_TOKEN_EXPIRE_MINUTES: int = 720` (12 hours) — `backend/app/core/config.py:100`; applied as the default `expires_delta` in `create_access_token` — `backend/app/core/security.py:72-73`. **No refresh-token mechanism exists** (grep for "refresh" finds only ORM `db.refresh`, none in auth).
- **Login flow.** `POST /auth/login` — `backend/app/api/auth.py:45-59`. Uses OAuth2 password form (`OAuth2PasswordRequestForm`), where the `username` field carries the **email**. It calls `user_service.authenticate_user` (`backend/app/services/user_service.py:104-117`: lookup by email, check `status==ACTIVE` + `is_active`, bcrypt `verify_password`), then mints a JWT via `create_access_token(subject=str(user.id), role=user.role)`. Returns `LoginResponse(access_token, token_type="bearer")`.
- **Identity resolution.** `get_current_user` (`backend/app/core/security.py:86-96`) → `_resolve_user_from_token` (`backend/app/core/security.py:105-127`): decode JWT (401 on `PyJWTError`), read `sub`, `int(sub)`, then `db.get(User, user_id)` against the **users table**; rejects missing / `status != ACTIVE` / `is_active == False` with 401. So every protected request hits the DB to resolve identity.
- **Bearer scheme.** `oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")` — `backend/app/core/security.py:29`. A second `auto_error=False` variant (`backend/app/core/security.py:102`) backs the `?token=` query-param fallback used only by the file-serve route (`get_current_user_query_or_header`, `backend/app/core/security.py:130-147`).
- **Admin gate.** `require_admin` checks `current_user.role != "admin"` → 403 (`backend/app/core/security.py:150-154`); `get_current_admin` is an alias (`backend/app/core/security.py:157-163`).
- **Registration gate.** `POST /auth/register` (`backend/app/api/auth.py:28-42`) is **public** and gated by `ALLOW_REGISTRATION`, which defaults to **`True`** (`backend/app/core/config.py:97`). Anyone can self-create a `role="user"` account when enabled.
- **Admin seed.** On every boot `_seed_admin_user` (`backend/app/main.py:37-55`) idempotently upserts a `role="admin"` user from `ADMIN_EMAIL` (default `admin@local`) + `ADMIN_PASSWORD` (default `admin123`), re-hashing the password and forcing role/active (`backend/app/services/user_service.py:138-194`). Dev defaults are insecure and must be overridden in prod (`backend/app/core/config.py:88-95`).

**Implication for Employee Zone:** the API today only trusts JWTs it issued itself (HS256 with its own secret), against its own users table. There is no way to trust an external SSO identity; an Employee Zone-issued token would not validate, and there is no JIT user-provisioning path.

---

## 2. CHAT — protected endpoints (`backend/app/api/chat.py`)

All five chat endpoints depend on `get_current_user` (full email/password JWT identity). Router has no prefix; paths are literal.

- `POST /chat/query` — non-streaming RAG answer + persistence (`backend/app/api/chat.py:50-87`); passes `owner_id=current_user.id` into `chat_service.query`.
- `POST /chat/stream` — SSE token stream + persistence (`backend/app/api/chat.py:90-133`); captures `owner_id = current_user.id` as a plain int before the generator runs.
- `POST /chat/conversations` — create empty conversation (201) (`backend/app/api/chat.py:136-155`).
- `GET /chat/conversations` — list caller's ACTIVE conversations (`backend/app/api/chat.py:158-166`).
- `GET /chat/conversations/{conversation_id}` — one conversation + messages, owner-scoped (404 on foreign) (`backend/app/api/chat.py:169-190`).
- `DELETE /chat/conversations/{conversation_id}` — soft-delete (`backend/app/api/chat.py:193-201`).

Plus the related search endpoint:
- `POST /search` — semantic search, `get_current_user`, `owner_id=current_user.id, include_global=True` (`backend/app/api/search.py:24-51`).

**SSE shape for mobile** (`backend/app/api/chat.py:114-133`): `media_type="text/event-stream"`; headers `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive` (`backend/app/api/chat.py:43-47`). Frames:
- `data: {"delta": "<token>"}\n\n` per token,
- terminal `data: {"done": true, "sources": [...], "conversation_id": N, "message_id": M}\n\n`,
- `data: {"error": "..."}\n\n` on failure.
Native mobile clients must consume raw SSE (line-based `data:` frames), not rely on a browser `EventSource`. The auth is the bearer header (POST body carries the query), which `EventSource` cannot send — fine for a native client doing a streaming POST.

---

## 3. KB SCOPING / OWNERSHIP — the real state

**`ownershipFilterPresent = TRUE.`** The multitenant plan in `MULTITENANT_PLAN.md` is **fully implemented** in the code (not just designed).

- **Document model** carries both columns: `owner_id: int | None` (FK `users.id`, indexed, NULL = not user-owned) — `backend/app/models/document.py:28-33`; `is_global: bool` (default false) — `backend/app/models/document.py:35-37`.
- **Default-deny SQL filter** is enforced in the vector store JOIN, not in Python: `_ownership_clause(owner_id, include_global)` builds `OR(Document.owner_id == owner_id, Document.is_global.is_(True))` — `backend/app/vectorstore/pgvector_store.py:33-46`. It is applied in **both** retrieval paths: `query` (`pgvector_store.py:88-128`, clause at line 128) and `keyword_query` (`pgvector_store.py:160-214`, clause at line 214). With `owner_id is None` and `include_global` false the candidate set is empty (no "all docs" fallback).
- **search_service.search** threads `owner_id` + `include_global` straight into `store.query` and `store.keyword_query` (`backend/app/services/search_service.py:358-414`, calls at lines 412-414 and 443-449). Returned hits carry `owner_id` / `is_global` from the resolved Document row (`search_service.py:597-599`).
- **chat_service** retrieves with `owner_id=owner_id, include_global=True` (`backend/app/services/chat_service.py:515-517`) and then `_build_sources` shows **only the user's own** non-global docs as visible sources — global docs ground the answer but never appear as a source chip (`backend/app/services/chat_service.py:296-335`, filter at line 314).

**For a shared company KB (admin-global docs only, employees cannot upload), what works today vs what is missing:**
- Works: an employee user (a `role="user"` account) querying gets `include_global=True`, so all `is_global=True` admin docs already ground every answer — exactly the shared-KB read model. Admin uploads global docs via `POST /documents/global`.
- Gap A: each employee also gets their **own** docs blended in (`Document.owner_id == owner_id` half of the OR clause). For a pure shared-KB-only experience you would query with `owner_id=None, include_global=True` (global-only). No endpoint currently calls search that way for an authenticated user.
- Gap B: employees can still **upload** personal docs (see §4), which contradicts the "cannot upload" requirement. Upload is open to any authenticated user.
- Gap C: `_build_sources` is built around showing the user's own docs; with a global-only KB the visible-sources rule would need to surface global docs instead (today globals are deliberately hidden from sources — `chat_service.py:296-314`).

---

## 4. UPLOAD — who can actually upload (`backend/app/api/documents.py`)

Two routers, both mounted in `backend/app/main.py:115-116`.

- **`public_router`** (`backend/app/api/documents.py:52-55`) — despite the name, **not anonymous**. Every route requires `get_current_user`. Upload endpoint:
  - `POST /documents/upload` (`backend/app/api/documents.py:118-151`) — any **authenticated user** can upload; the doc is owned by them (`owner_id=current_user.id, is_global=False`, set server-side, line 144-146). **This is the route employees must NOT be allowed to hit.**
- **`admin_router`** (`backend/app/api/documents.py:58-62`, `dependencies=[Depends(get_current_admin)]`) — admin-only:
  - `POST /documents/global` (`backend/app/api/documents.py:243-266`) — admin-only global upload (`owner_id=None, is_global=True`, line 261-262). This is the correct path for admin to populate the shared company KB.

Other document routes on `public_router` (all `get_current_user`, owner-scoped via `_owner_scope`, admin sees all): `GET /documents` list (`:273`), `GET /documents/{doc_id}` (`:292`), `DELETE /documents/{doc_id}` (`:305`), `POST /documents/{doc_id}/reindex` (`:320`), and `GET /documents/{doc_id}/file` (`:154`, auth via header OR `?token=` query param). The historic comment says anonymous upload is gone — confirmed (`backend/app/api/documents.py:49-51`, `122-123`).

**Upload endpoints:** `POST /documents/upload` (any authenticated user — must be blocked for employees), `POST /documents/global` (admin only — correct for KB population).

---

## 5. TRANSPORT / CORS

- CORS middleware: `allow_origins=settings.CORS_ORIGINS`, `allow_credentials=True`, `allow_methods=["*"]`, `allow_headers=["*"]` — `backend/app/main.py:83-89`.
- `CORS_ORIGINS` defaults to `["http://localhost:3000"]` (CSV/JSON-parsed from env) — `backend/app/core/config.py:86`, validator `backend/app/core/config.py:111-132`.
- **Note:** `allow_credentials=True` combined with an explicit origin list is valid; it would be invalid only with a wildcard `*` origin. The current default is a fixed origin, so credentials mode is fine but the allowlist must be extended for any web-based origin.

**Implication for the native mobile client:** a native iOS/Android HTTP client does **not** enforce CORS (it is a browser same-origin policy), so `CORS_ORIGINS` is irrelevant to direct native calls — they will work regardless of the allowlist. CORS only matters if the Employee Zone uses a **WebView / web** front-end calling the API from a browser context, in which case the WebView's origin must be added to `CORS_ORIGINS`. Authentication is via the `Authorization: Bearer` header, not cookies, so `allow_credentials` is not load-bearing for the mobile path.

---

## 6. OPS / LIMITS — providers, quota, rate limiting

- **Providers (config defaults).** `EMBEDDING_PROVIDER: str = "local"`, `LLM_PROVIDER: str = "local"` (`backend/app/core/config.py:33-34`). The deployed branch overrides these to Gemini (per repo history), but the code default is local. Keys: `GEMINI_API_KEY` + optional `GEMINI_API_KEY_FALLBACK` (rotated on 429) (`backend/app/core/config.py:38-42`); `GEMINI_MODEL` default `gemini-2.5-flash` (`backend/app/core/config.py:43`).
- **Gemini embedding pacing.** The free-tier `embedContent` endpoint caps near 15 RPM, so requests are paced under a budget: `GEMINI_EMBED_RPM: int = 10`, `GEMINI_EMBED_MAX_RETRIES: int = 6`, exponential backoff `GEMINI_EMBED_BACKOFF_BASE_SECONDS = 0.5` up to `GEMINI_EMBED_MAX_BACKOFF_SECONDS = 60.0` (`backend/app/core/config.py:50-53`). `INDEXING_STALE_MINUTES: int = 120` accommodates slow paced jobs (`backend/app/core/config.py:57`). Real free-tier ceiling is ~1000 embed requests/**day** (per project memory) — relevant only to indexing, which is admin-side.
- **LLM (chat) 429 handling.** Gemini chat calls retry with backoff and rotate across the key ring on 429 / RESOURCE_EXHAUSTED, for both non-streaming and streaming (`backend/app/rag/llm.py:521-568`, `625-637`, `755-771`). On exhaustion the chat pipeline falls back to a deterministic local extractive provider rather than 500 (`backend/app/services/chat_service.py:338-385`).
- **Application rate limiting: NONE.** There is no `slowapi`/limiter/throttle middleware anywhere in `app/` (grep confirms zero hits outside the embed-pacing constants). There is **no per-user or per-IP request limiting** on `/chat/*` or `/search`. The only protection against load is the provider-side 429 + local fallback.

**Implication for many concurrent employees:** with Gemini providers, concurrent chat traffic will share one (or two) free-tier API keys and hit RPM/daily limits quickly, silently degrading to the weaker local extractive answer. There is no fair-use throttle to protect the shared quota, and no per-employee budget.

---

## 7. GAPS — prioritized changes required for Employee Zone integration

**(a) Trust an Employee Zone SSO identity instead of a second email/password login**
1. Add a trusted-token verification path: accept the Employee Zone-issued token (verify signature/issuer/audience — if RS256/JWKS, add asymmetric verification; current code is HS256-only, `backend/app/core/security.py:82`).
2. Add JIT user provisioning: on first valid SSO token, upsert a `User` row (the identity model is id-in-`sub` resolved against the users table — `security.py:105-127`), or maintain a stable mapping from the SSO subject/employee-id to a local user id.
3. Disable the self-service login surface for employees: `POST /auth/login` (`api/auth.py:45`) and self-registration `POST /auth/register` (set `ALLOW_REGISTRATION=False`, `config.py:97`) should not be the employee entry point.
4. Replace/augment `JWT_SECRET_KEY` HS256 with the SSO provider's verification material; keep the internal admin login if admins still log in directly.

**(b) Shared company KB — employees read global docs only, cannot upload**
5. Provide a global-only retrieval mode for employees: call `search_service.search(..., owner_id=None, include_global=True)` (or a role/flag-driven switch) so employees never blend in personal docs. The SQL clause already supports global-only (`pgvector_store.py:33-46`).
6. Block employee uploads: remove/deny `POST /documents/upload` for the employee role (currently any authenticated user — `documents.py:118-124`). Keep `POST /documents/global` admin-only (already correct).
7. Adjust the visible-sources rule for a global KB: today `_build_sources` hides global docs (`chat_service.py:296-314`); employees should see the global doc as the cited source. Decide and implement the source-display policy for global-grounded answers.
8. Decide on personal artifacts: conversations are per-user (`Conversation.user_id`, `models/conversation.py:23`) — keep, but confirm the employee identity maps consistently so history follows the employee.

**(c) Native mobile client calling directly (token lifecycle, refresh, rate limiting)**
9. Token lifecycle / refresh: there is **no refresh token** and access tokens last 12h (`config.py:100`); a native app needs either silent SSO re-auth or a refresh mechanism so sessions don't hard-expire mid-use.
10. Per-employee rate limiting: add request throttling (none today) so concurrent employees don't exhaust the shared Gemini free-tier quota and silently fall back to the weak local LLM (`chat_service.py:338-385`); consider per-user quotas and provider-key scaling.
11. SSE consumption contract: document the raw SSE frame shapes (`delta` / `done` / `error`, `chat.py:122-127`) for the native client; it must POST with a bearer header and parse line-based SSE (not browser `EventSource`).
12. CORS is a non-issue for native direct calls (browser-only); only add the origin to `CORS_ORIGINS` (`config.py:86`) if a WebView/web surface calls the API.
13. Production hardening: override `JWT_SECRET_KEY`, `ADMIN_PASSWORD`, `ADMIN_EMAIL` dev defaults (`config.py:88-99`); they are insecure placeholders.

---

## Files read for this audit
- `backend/app/main.py`
- `backend/app/api/auth.py`
- `backend/app/api/chat.py`
- `backend/app/api/documents.py`
- `backend/app/api/search.py`
- `backend/app/api/admin.py`
- `backend/app/core/security.py`
- `backend/app/core/config.py`
- `backend/app/services/chat_service.py`
- `backend/app/services/search_service.py`
- `backend/app/services/user_service.py`
- `backend/app/models/document.py`
- `backend/app/models/user.py`
- `backend/app/models/conversation.py`
- `backend/app/models/chunk.py`
- `backend/app/vectorstore/pgvector_store.py` (ownership-clause verification)
- `backend/app/rag/llm.py` (quota / 429 handling)
- `MULTITENANT_PLAN.md`
