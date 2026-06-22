# 04 — Mobile Transport, CORS, Streaming, Rate-Limiting & Ops

> Scope: how a **native mobile app** (Employee Zone) calls the RAG API directly. Spec/design only — no application code changed. Grounded in the current code (`main.py`, `config.py`, `api/chat.py`, `rag/llm.py`, `utils/errors.py`).

## Recommendation (summary)

For the native mobile app, **CORS needs no change** (native HTTP stacks do not enforce CORS; `CORS_ORIGINS` only matters for a webview/companion-web build, and `allow_origins='*'` must never combine with `allow_credentials=True`).

- Default the client to **`POST /chat/query`** (non-streaming) for robustness. Offer `POST /chat/stream` only as an opt-in **foreground** enhancement: its SSE is a Bearer POST (not `EventSource`-compatible), has no `Last-Event-ID` resume, and reconnect-replay re-spends shared Gemini quota.
- **Highest-priority backend gap:** add app-level **per-user (JWT `sub`) + global daily rate limiting** (slowapi/Redis) and **stop the silent fall-back to the weak local LLM** on Gemini quota exhaustion (`chat_service.py:338-385`) — surface a real `429` with `Retry-After` (a `RateLimitError → 429` already exists in `errors.py`) or a `degraded` flag, and move chat to a **paid Gemini tier** for a fleet.
- Add `/api/v1` versioning, a stable error envelope, conversation/message pagination, `Idempotency-Key` on chat POSTs, query-PII log redaction, and a `/health` keep-warm pinger for Render cold starts.

## Options considered

1. **Default mobile transport = `POST /chat/query` (non-streaming)** — robust to backgrounding/proxy idle timeouts, simple JSON decode, no quota-amplifying reconnect-replay. **RECOMMENDED.**
2. **`POST /chat/stream` as opt-in foreground "live typing" only** — native Bearer POST + manual SSE frame parsing (`delta`/`done`/`error`), hard client timeout, fall back to `/chat/query` on drop.
3. **Rate limiting:** slowapi (per-user keyed on JWT `sub` + global daily cap, Redis-backed) vs custom ASGI/dependency middleware.
4. **Provider posture:** move chat to a **paid Gemini tier** (capacity) keeping key-ring rotation + local LLM only as an observable emergency fallback — vs stay on free tier with a hard global cap + 429 shedding.
5. **Versioning:** `/api/v1` path prefix (recommended, mobile-pinnable) vs `Accept-Version` header.
6. **CORS:** no change for native path; only add explicit web/webview origins to `CORS_ORIGINS` if such a surface ships (never `'*'` with credentials).

## CORS

- Native (non-webview) HTTP clients **ignore CORS** — it is a browser-only control. Do **not** treat `CORS_ORIGINS` as a security boundary; the security boundary is the **Bearer JWT** (`security.py:105-127`).
- Current config: `allow_origins=settings.CORS_ORIGINS`, `allow_credentials=True`, `allow_methods/headers=*` (`main.py:83-89`), default `['http://localhost:3000']` (`config.py:86`).
- Only add explicit web/webview origins if such a surface ships. **Never** combine `allow_origins=['*']` with `allow_credentials=True` (spec-forbidden; CSRF-class risk).

## Streaming on mobile

- `/chat/stream` emits line frames: `data:{"delta":...}` / `data:{"done":true,"sources":[...],"conversation_id":N,"message_id":M}` / `data:{"error":...}` (`chat.py:122-127`). It is a **Bearer POST**, not browser `EventSource`.
- Backgrounding / idle / proxy timeouts truncate SSE; there is **no `Last-Event-ID` resume**, and reconnect re-runs the pipeline and **re-spends Gemini quota**.
- **Default to `/chat/query`**; use streaming only in the foreground with a hard inter-frame idle timeout (~30s) + overall cap, falling back to `/chat/query` on drop.

## Rate limiting / abuse (highest-priority gap)

- There is **no app-level rate limiting today**. Concurrent employees or one abusive token can burn the shared Gemini free-tier daily quota fast.
- Add per-user (JWT `sub`) + global daily caps on `/chat/query`, `/chat/stream`, `/search`; raise `RateLimitError`/`429` with `Retry-After`; exclude GETs and `/health`.
- **Stop silent quota degradation:** on Gemini all-keys 429, return `429` (preferred) or tag the response `degraded:true` / `X-Answer-Source: local-fallback` so the client can warn the user (`chat_service.py:338-385`).
- For a fleet, move chat to a **paid Gemini tier**; keep key-ring rotation + local LLM only as an **observable** emergency fallback.

## API ergonomics for mobile

- **Versioning:** add `/api/v1` path prefix (optionally keep unversioned aliases for the web build during transition).
- **Error envelope:** standardize a stable `code` enum; make `detail` always a string for non-422 (today it is sometimes a string, sometimes a list — brittle for a typed decoder, `errors.py:84-86,68-91`). Client maps: `401→re-auth`, `404→gone`, `422→fix input`, `429→back-off (Retry-After)`, `5xx→tap-to-retry`.
- **Pagination:** add `limit`/`offset` (or cursor) to `GET /chat/conversations` and to message retrieval (`chat.py:158-190`).
- **Idempotency:** accept an `Idempotency-Key` header on chat POSTs and dedupe within a TTL; auto-retry GETs only, never chat POSTs blindly.
- **Request guards:** `MAX_QUERY_CHARS`, `MAX_HISTORY_TURNS` in `ChatRequest` + a global request-body byte cap.
- **Discovery:** generate typed client models from `/openapi.json`; `/docs` (Swagger) for humans (`main.py:93-101`).
- **Timeouts:** `/chat/query` read ~60s (Gemini `_TIMEOUT=60.0`); `/chat/stream` inter-frame idle ~30s + hard cap; GETs ~10s.

## Ops / observability

- `/health` exists (GET+HEAD). Add an external **keep-warm pinger** on `HEAD /health` (or an always-on instance) to kill Render cold starts (a cold start on a streaming POST looks like a hang).
- Add request-id structured logging (return a `request_id` header), per-route latency + 429/5xx counters, and a metric counting local-fallback (degraded) answers; optional deep `/readyz`.
- **PII:** redact/hash or `DEBUG`-gate the raw user-query logging (`chat.py:65-70, 107-112`); do not log Bearer tokens or full answers.
- Gate `/docs` and `/openapi.json` in production (`EXPOSE_DOCS=False`) to avoid advertising the full surface.

## New config/env settings (spec only)

`API_VERSION_PREFIX`, `CHAT_RATELIMIT_PER_USER`, `CHAT_DAILY_PER_USER`, `CHAT_RATELIMIT_GLOBAL`, `MAX_QUERY_CHARS`, `MAX_HISTORY_TURNS`, `MAX_REQUEST_BYTES`, `RATELIMIT_BACKEND_URL`, `EXPOSE_DOCS`, `LOG_QUERIES`.

## API contract (mobile-facing)

- **Auth:** `Authorization: Bearer <jwt>` on every protected call (no cookies; CORS irrelevant to native).
- **Primary chat (default):** `POST /api/v1/chat/query` `{query, top_k?, history?, conversation_id?}` → `{answer, sources[], conversation_id, message_id}` (`ChatResponse`, `chat.py:50-87`).
- **Optional stream:** `POST /api/v1/chat/stream`, `Accept: text/event-stream`; parse the frame shapes above; opt-in, foreground-only.
- **Conversations:** `POST /chat/conversations` (201, returns id); `GET /chat/conversations?limit&offset`; `GET /chat/conversations/{id}` (paged messages); `DELETE /chat/conversations/{id}`.
- **Errors:** stable envelope with `code` enum + the client status map above.
- **Idempotency:** `Idempotency-Key` on chat POSTs.

## Security notes

- Native clients ignore CORS; the boundary is the Bearer JWT (`security.py:105-127`).
- Never `allow_origins=['*']` + `allow_credentials=True`; with credentials, origins MUST be explicit (`main.py:86`).
- TLS only. Do not log Bearer tokens, raw queries (PII), or full answers.
- Rate limiting is also an abuse/DoS control — a single compromised token could otherwise drain the shared Gemini key.
- Gate `/docs` + `/openapi.json` in production (`EXPOSE_DOCS=False`).
- Map the in-band `data:{"error":...}` frame to a generic client message; do not leak internal detail.
- The API trusts only its own HS256 JWTs; an external SSO token will not validate (see `02-auth-sso-design.md`).

## Risks

1. **Silent quota degradation (highest):** shared free-tier exhaustion drops to the weak local LLM with no client signal (`chat_service.py:338-385`).
2. **No app-level rate limiting today:** concurrent/abusive tokens burn the daily Gemini quota fast.
3. **Streaming fragility on mobile:** backgrounding/idle/proxy timeouts truncate SSE; no resume; reconnect re-spends quota.
4. **Non-idempotent chat POSTs:** timeout-driven retries double-post and double-spend until `Idempotency-Key` exists.
5. **Render cold start:** first-request latency after idle looks like a hang on a streaming POST.
6. **Unbounded conversation/message lists:** no pagination grows payloads (`chat.py:158-190`).
7. **Error-envelope type instability:** `detail` sometimes string, sometimes list (`errors.py:84-86`).
8. **PII in logs:** raw queries logged at INFO (`chat.py:65-70, 107-112`).
9. **No app version boundary:** unversioned paths make a breaking change hard once shipped.
