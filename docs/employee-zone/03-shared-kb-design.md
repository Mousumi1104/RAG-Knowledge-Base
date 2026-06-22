# Employee Zone — Shared Knowledge Base Design (global-only, read-only)

Scope: a SPEC/DESIGN for guaranteeing that an **Employee Zone** user (the `role="user"`
account class, accessed via SSO) can query **ONLY** the shared company knowledge base —
the admin-uploaded `is_global=True` documents — and can **NEVER** upload, list, or retrieve
any other user's private documents. **No application code is modified by this document.**
Every claim below is grounded in the current source (file:line).

Confirmed hard requirements (from the requester):
- Employees can **ONLY ask questions**. They cannot upload documents at all.
- Employees retrieve from the **shared company KB only** (no personal-doc blend).
- Admins continue to curate the shared KB (`POST /documents/global`).

---

## 0. Where we start (current code, verified)

- **Ownership filter is real and default-deny.** `_ownership_clause(owner_id, include_global)`
  builds `OR(Document.owner_id == owner_id, Document.is_global.is_(True))`, returns literal
  `false()` when `owner_id is None` **and** `include_global is False`
  (`backend/app/vectorstore/pgvector_store.py:33-50`). It is applied in **both** retrieval
  SQL paths — vector `query` (`pgvector_store.py:124-129`) and lexical-rescue `keyword_query`
  (`pgvector_store.py:210-215`). This is the single chokepoint for tenant isolation.
- **search_service.search** threads `owner_id` + `include_global` straight into
  `store.query` and `store.keyword_query` (`backend/app/services/search_service.py:358-365`,
  calls at `412-414` and `443-449`); resolved hits carry `owner_id`/`is_global`
  (`search_service.py:597-599`). It already documents global-only support:
  *"With `None` and `include_global` False the candidate set is empty"* (`search_service.py:378`).
- **chat_service** is the only retrieval caller for chat, and it **hardcodes**
  `owner_id=owner_id, include_global=True` (`backend/app/services/chat_service.py:515-517`) for
  BOTH `query()` and `stream_answer()` (both call `_retrieve_and_build_prompt`,
  `chat_service.py:664-666` and `763-765`). So today an employee's own docs are blended in.
- **`_build_sources`** deliberately **hides** global docs: a hit is shown as a source iff
  `is_global == False AND owner_id == <owner_id>` (`chat_service.py:296-314`, skip at line 314).
  For a global-only KB this hides *every* source.
- **Uploads.** `POST /documents/upload` is open to **any authenticated user** and stamps
  `owner_id=current_user.id, is_global=False` server-side (`backend/app/api/documents.py:118-151`).
  `POST /documents/global` is admin-only via the router-level
  `dependencies=[Depends(get_current_admin)]` (`documents.py:58-62`, `243-266`).
- The "public_router" is **not anonymous** — every route already requires
  `get_current_user` (`documents.py:45-55`); the name is historical. Anonymous upload is gone.
- **Admin gate** is role-based: `require_admin` → 403 when `current_user.role != "admin"`
  (`backend/app/core/security.py:150-154`); `get_current_admin` is its alias (`security.py:157-163`).
- `POST /search` also hardcodes `owner_id=current_user.id, include_global=True`
  (`backend/app/api/search.py:43-49`).

So the SQL layer **already supports** the global-only mode we need; the gap is purely that
no chat/search caller invokes it that way, plus the source-hiding rule and the open upload route.

---

## 1. Constrain retrieval to `is_global=true` ONLY (global-only mode)

### Decision

Use a **role/claim-driven policy**, computed once per request from the authenticated
identity, that resolves into the **existing `(owner_id, include_global)` retrieval scope**.
It is NOT a client-supplied per-request flag (a flag the client controls is a privilege-
escalation vector) and it is NOT a brand-new SQL filter (the SQL already supports global-only).

Concretely, define a single helper that maps a `User` to a retrieval scope:

```
# proposed: backend/app/core/access.py  (NEW small module)
GLOBAL_ONLY = (None, True)        # owner_id=None, include_global=True  -> globals only
def retrieval_scope(user) -> tuple[int | None, bool]:
    # Employee Zone users (role == "user") read the shared KB ONLY.
    if user.role == "admin":
        return (user.id, True)    # admin keeps own + global (unchanged behavior)
    return GLOBAL_ONLY            # employee: globals only, NO personal blend
```

`GLOBAL_ONLY = (None, True)` feeds the **already-implemented** default-deny clause: with
`owner_id=None, include_global=True` the clause reduces to `Document.is_global.is_(True)`
(`pgvector_store.py:43-50`) — exactly "global documents and nothing else". No other tenant's
chunk, and no personal chunk, can enter the vector pool, the keyword-rescue pool, or the
results. This is the strongest possible guarantee because it is enforced in SQL, not Python.

### Where it must be applied (so BOTH /chat/query and /chat/stream are covered)

The scope must be resolved at the **API boundary** (where the `User` is known) and threaded
down as two plain values, because both chat entrypoints already pass `owner_id` as a plain int
across the streaming session boundary (`chat.py:106`, `chat_service.py:598-606`). Plan:

1. **API layer (`backend/app/api/chat.py`)** — compute `owner_id, include_global = retrieval_scope(current_user)`
   in `chat_query` (`chat.py:50-79`) and `chat_stream` (`chat.py:90-121`), and pass BOTH into
   `chat_service.query(...)` / `chat_service.stream_answer(...)`.
2. **`chat_service.query` / `stream_answer`** (`chat_service.py:700-708`, `591-598`) — add an
   `include_global: bool = True` keyword param alongside the existing `owner_id`, and forward
   it into `_retrieve_and_build_prompt`.
3. **`chat_service._retrieve_and_build_prompt`** (`chat_service.py:501-517`) — replace the
   hardcoded `include_global=True` (line 516) with the threaded value, and pass the threaded
   `owner_id` (which is `None` for employees) into `search_service.search`.
4. **`search_service.search`** needs **no change** — it already accepts and forwards
   `owner_id`/`include_global` to both SQL paths. The `pgvector_store` needs **no change**.

Because BOTH `query()` and `stream_answer()` funnel through the single
`_retrieve_and_build_prompt`, fixing that one function covers `/chat/query` **and**
`/chat/stream` with no divergence.

`/search` (`backend/app/api/search.py:43-49`) must get the **same** treatment (resolve scope
via `retrieval_scope`) so the search endpoint cannot be used as a side channel to read personal
docs. If `/search` is not exposed to Employee Zone at all, lock it behind admin instead
(see §3); do not leave it on the open `owner_id=current_user.id` path for an SSO employee.

### Why a policy, not a client flag

If a client could send `include_global=false` or `owner_id=<n>`, an employee could attempt to
target another tenant. By deriving scope **only** from the server-verified identity/role, the
client has zero influence over what it can read. Ownership/scoping is never client-controlled
(consistent with the existing upload rule that stamps `owner_id` server-side, `documents.py:144`).

---

## 2. Chat read endpoints — access control + showing global sources

### Conversations are already correctly owner-scoped (keep as-is)

All five chat endpoints depend on `get_current_user` (`chat.py:50-201`), and every
conversation/message operation is scoped by `Conversation.user_id == owner_id`:
- create (`chat_service.create_conversation`, `chat_service.py:445-458`),
- list (`list_conversations`, `chat_service.py:535-545`),
- get (`get_conversation_or_404` → `_get_active_conversation`, `chat_service.py:399-412`, `548-557`),
- delete (`soft_delete_conversation`, `chat_service.py:578-588`),
- resolve/attach (`resolve_or_create_conversation`, `chat_service.py:415-442`).

A foreign/missing conversation id 404s identically (no existence leak, `chat_service.py:548-557`).
**No change needed** — an employee can create/list/get/delete only their own conversations.

### Make global-doc citations visible to employees (the required `_build_sources` change)

Today `_build_sources` hides global docs (`chat_service.py:296-314`). In global-only mode the
**only** docs grounding the answer are global, so the current rule yields an empty sources list
for every employee answer. The requirement is that the **global doc that grounded the answer IS
shown** as the citation.

Proposed change — make the source-visibility rule **scope-aware** rather than a fixed
"own-docs-only". `_build_sources` should accept the resolved scope (or an explicit
`show_global: bool`) and:

- **Employee / global-only scope** (`owner_id is None`): show **global** hits as sources
  (`is_global == True`), and NEVER show any non-global hit. Since retrieval already returned
  only global hits in this mode, this surfaces exactly the grounding global document(s).
- **Admin / own+global scope** (unchanged): keep the existing own-docs-only display, OR also
  show globals — admin display policy is an explicit choice but does not affect employee safety.

Sketch (the predicate becomes scope-driven; everything else in `_build_sources` is unchanged —
dedup by `(document_id, excerpt)`, no score emitted, retrieval order preserved):

```
def _build_sources(hits, owner_id, *, show_global: bool):
    for hit in hits:
        is_global = bool(hit.get("is_global"))
        if show_global:
            # global-only employee mode: only global docs are valid citations
            if not is_global:
                continue
        else:
            # existing own-docs-only rule (admin / personal mode)
            ho = hit.get("owner_id")
            if is_global or ho is None or int(ho) != int(owner_id):
                continue
        ...                                  # emit {document, excerpt, document_id, page}
```

`_retrieve_and_build_prompt` (`chat_service.py:532`) passes `show_global = (owner_id is None)`
(i.e. employee scope) into `_build_sources`. The downstream "drop sources when the answer is a
not-found disclaimer" logic (`chat_service.py:678-679`, `770-771`) is unchanged and still applies.

**Security note on this change:** widening sources to globals is safe precisely BECAUSE
retrieval in employee mode can only ever return global hits (§1) — there is no way for a
personal or foreign chunk to be in `hits`, so there is nothing unsafe to surface. The visible-
sources rule is a *display* refinement layered on top of the SQL isolation, never a substitute
for it.

The `Source` schema and the SSE `done` frame (`chat.py:124-125`) are unchanged; employees now
simply receive non-empty `sources` arrays pointing at global KB documents.

---

## 3. Block ALL employee uploads and document management

Default rule for Employee Zone: **employees get chat read-only.** Every document-management
endpoint that today accepts `get_current_user` (i.e. any logged-in user) must be made
**admin-only** for this deployment, because there is no legitimate employee use of any of them.

### Endpoints to restrict to admin-only (via `require_admin` / `get_current_admin`)

All currently on `public_router` with `Depends(get_current_user)`:

| Endpoint | Current dep | Change |
|---|---|---|
| `POST /documents/upload` (`documents.py:118-124`) | `get_current_user` | **`get_current_admin`** — employees must NOT upload. |
| `GET /documents` (`documents.py:273-277`) | `get_current_user` | `get_current_admin` — list is admin-only. |
| `GET /documents/{doc_id}` (`documents.py:292-296`) | `get_current_user` | `get_current_admin`. |
| `DELETE /documents/{doc_id}` (`documents.py:305-309`) | `get_current_user` | `get_current_admin`. |
| `POST /documents/{doc_id}/reindex` (`documents.py:320-325`) | `get_current_user` | `get_current_admin`. |
| `GET /documents/{doc_id}/file` (`documents.py:154-158`) | `get_current_user_query_or_header` | Keep authenticated, but restrict to **global docs for employees** — see below. |

**Confirmed: `POST /documents/upload` is denied to the employee role.** With the upload route
moved behind `require_admin`, an employee's SSO token (role `"user"`) hits 403
(`security.py:150-154`). The route also stamps `owner_id`/`is_global` server-side, so even if it
were reachable there is no client-controlled escalation path to mint a global doc — only
`POST /documents/global` can, and it is already admin-only.

**Two clean ways to implement** (either is acceptable; the second is structurally safer):

- **(a) Per-route dep swap** — change each `Depends(get_current_user)` above to
  `Depends(get_current_admin)`. Minimal diff, but relies on every future route remembering it.
- **(b) Move the management routes onto the existing `admin_router`** (which already carries
  `dependencies=[Depends(get_current_admin)]` at the router level, `documents.py:58-62`), leaving
  on `public_router` only what employees legitimately need. **Recommended** — it makes
  "admin-only by default" the structural default, so a new management route added to the admin
  router is guarded automatically. Watch the route-ordering note (`documents.py:114-117`):
  literal paths (`/upload`, `/global`) must be declared before `/{doc_id}` style routes on the
  same router to avoid shadowing.

### The `/documents/{doc_id}/file` route (source-link rendering)

Employees DO need to open a cited global document (the source chips from §2 link to it). Keep
this route authenticated, but for an employee the ownership scope must be **global-only**, not
`_owner_scope(current_user)` (which today returns `current_user.id` and would also serve the
employee's own files — irrelevant once uploads are blocked, but still the wrong scope).

- Replace `_owner_scope(current_user)` (`documents.py:179`) with the §1 scope: for an employee
  pass `owner_id=None` so `document_service.get_document(..., allow_global=True)` serves the file
  **only if `is_global` is true** (or the doc is owned by the caller — which, for an upload-less
  employee, is the empty set). A non-global / foreign id 404s identically (`documents.py:181-182`),
  preserving the no-existence-leak guarantee. Admin keeps `owner_id=None` = all docs (note the
  semantics differ by role; the §1 helper must distinguish "admin: all" from "employee: globals
  only", e.g. `get_document` is called with an explicit `globals_only` for employees).

### The "public_router" / anonymous-upload router

There is **no anonymous-upload router** left to disable: `public_router` already requires
`get_current_user` on every route, and the historical anonymous path is gone
(`documents.py:45-55`, `122-123`). For this deployment, additionally:

- Confirm `ALLOW_REGISTRATION=False` in prod so no one can self-mint a `role="user"` account
  outside SSO (`backend/app/core/config.py:97`; current default is `True` — must be overridden).
  Employee identities come from the trusted SSO/JIT path, not self-registration.
- The router *name* `public_router` is misleading; renaming is cosmetic and optional, but the
  design intent is: **`public_router` should carry only chat-read-adjacent, employee-safe
  routes** (in practice only the global file-serve), and **everything that writes or lists docs
  lives on `admin_router`.**

---

## 4. Admin KB curation + embedding/quota implications

Admins remain the sole curators of the shared KB: they upload/refresh global docs via
`POST /documents/global` (`documents.py:243-266`, `owner_id=None, is_global=True`) and reindex via
the (now admin-only) `POST /documents/{doc_id}/reindex`. Indexing runs as a background task
(`_run_indexing_bg`, `documents.py:75-110`) so large uploads don't time out the request.

### Quota reality (Gemini free tier)

- Embedding is the bottleneck, and it is **admin-side** (only admins index global docs). The
  free-tier `embedContent` endpoint is paced under a budget — `GEMINI_EMBED_RPM=10`, retries
  with exponential backoff (`GEMINI_EMBED_MAX_RETRIES=6`, base `0.5s`, cap `60s`), and
  `INDEXING_STALE_MINUTES=120` to tolerate slow paced jobs (`backend/app/core/config.py:50-57`).
- The **real ceiling is ~1000 embed requests per DAY** on the free tier (per project memory:
  *rag-gemini-embed-daily-cap-and-pacing-fix*). A large shared KB is many chunks → many embed
  calls; e.g. an 80-page handbook chunked at ~500 chars is on the order of hundreds of chunks =
  hundreds of embed requests, so **one or two big handbooks can exhaust the daily cap**.
- A half-migrated index is a correctness hazard, not just a speed one: if global docs are NOT
  Gemini-reembedded but employee **queries** are embedded with Gemini, only keyword hits work
  and semantic/paraphrased queries return nothing (per memory: *rag-live-halfmigrated-index*).
  The query embedder and the stored chunk embedder MUST be the same provider/dimension.

### Ops guidance (recommended)

1. **Index the shared KB during off-hours, batched across days** if it is large, staying under
   the ~1000/day embed cap; verify `chunk_count` and that `indexing_status="indexed"` for every
   global doc before exposing Employee Zone.
2. **Verify full reembedding after any provider switch** — every global chunk must carry a
   Gemini embedding of the configured `EMBEDDING_DIM`; otherwise employees get silent empty
   answers for semantic queries. Use a one-off verify of embedding coverage on the global docs.
3. **Provision a paid/higher embedding tier (or a key ring)** before scaling the KB; the free
   daily cap is unsuitable for a large, frequently-refreshed company KB.
4. **De-duplicate global uploads** — duplicate global PDFs double the chunk/embed count and
   waste quota (per memory: duplicate-PDF indexing). Refresh = delete-then-reupload or reindex,
   not re-upload alongside the old copy.
5. **Chat-time quota is separate**: many concurrent employees share the Gemini chat key and will
   hit RPM/daily limits, silently degrading to the weak local extractive fallback
   (`chat_service.py:338-385`). There is **no application rate limiting** today; add per-employee
   throttling/quota so the shared key isn't exhausted (out of scope for KB isolation, but a real
   operational risk for a many-employee rollout).

---

## 5. changeList / apiContract / securityNotes / risks

### changeList (files / functions to change)

- **`backend/app/core/access.py` (NEW)** — `retrieval_scope(user) -> (owner_id, include_global)`
  helper; constant `GLOBAL_ONLY = (None, True)`. Single source of truth for "employees = globals
  only, admins = own+global". (A global-only scope helper, as required.)
- **`backend/app/services/search_service.search`** — NO code change (already accepts/forwards
  `owner_id`/`include_global` to both SQL paths). Listed to confirm it is the threading point.
- **`backend/app/services/chat_service._retrieve_and_build_prompt`** (`chat_service.py:501-517`,
  `532`) — stop hardcoding `include_global=True`; accept and forward `owner_id`/`include_global`
  from the scope; pass `show_global=(owner_id is None)` into `_build_sources`.
- **`backend/app/services/chat_service.query` / `stream_answer`** (`chat_service.py:700-708`,
  `591-598`, `664-666`, `763-765`) — add `include_global` kwarg; forward into
  `_retrieve_and_build_prompt`.
- **`backend/app/services/chat_service._build_sources`** (`chat_service.py:296-335`) — make the
  source-visibility predicate scope-aware (`show_global` param) so global citations ARE shown to
  employees; preserve dedup/order/no-score behavior.
- **`backend/app/api/chat.py`** (`chat_query` `:50-79`, `chat_stream` `:90-121`) — resolve
  `owner_id, include_global = retrieval_scope(current_user)` and pass both into chat_service.
- **`backend/app/api/search.py`** (`search` `:43-49`) — resolve scope via `retrieval_scope`
  (or move endpoint behind admin if not exposed to employees).
- **`backend/app/api/documents.py`** — restrict `POST /documents/upload`, `GET /documents`,
  `GET /documents/{doc_id}`, `DELETE /documents/{doc_id}`, `POST /documents/{doc_id}/reindex`
  to admin (swap deps to `get_current_admin`, or move onto `admin_router`); change
  `GET /documents/{doc_id}/file` scope to global-only for employees (replace `_owner_scope`
  use at `documents.py:179`).
- **Config / ops** — `ALLOW_REGISTRATION=False` in prod (`config.py:97`); override
  `JWT_SECRET_KEY`/`ADMIN_PASSWORD`/`ADMIN_EMAIL` dev defaults (`config.py:88-99`).

### apiContract (endpoint behavior changes)

- `POST /chat/query`, `POST /chat/stream` — for an employee (`role="user"`): answers are grounded
  **only** on `is_global=True` documents; personal docs are never blended. `sources[]` now
  contains the **global** documents that grounded the answer (previously empty for globals).
  Shape of `Source`/SSE frames unchanged.
- `POST /search` — employee scope becomes global-only (or endpoint becomes admin-only). No
  personal-doc results for employees.
- `POST /documents/upload` — returns **403** for employees (admin-only). (Was 201 for any user.)
- `GET /documents`, `GET /documents/{doc_id}`, `DELETE /documents/{doc_id}`,
  `POST /documents/{doc_id}/reindex` — **403** for employees (admin-only).
- `GET /documents/{doc_id}/file` — employees may open **global** docs only; any non-global/foreign
  id returns **404** (unchanged no-existence-leak behavior).
- `POST /documents/global` — unchanged (admin-only KB curation).
- Conversation CRUD (`/chat/conversations*`) — unchanged; still per-employee owner-scoped.

### securityNotes (data-isolation guarantees + how to TEST)

Guarantees:
- **SQL default-deny is the primary isolation.** Employee retrieval uses `owner_id=None,
  include_global=True`, which reduces `_ownership_clause` to `is_global IS TRUE`
  (`pgvector_store.py:43-50`). No personal/foreign chunk can enter the vector pool, the
  keyword-rescue pool (`pgvector_store.py:210-215`), or the results — isolation is enforced in
  the JOIN, not in Python.
- **Scope is server-derived from role/identity**, never client-supplied → no escalation via
  request params (consistent with server-side `owner_id` stamping on writes, `documents.py:144`).
- **Source visibility is a display refinement** layered on SQL isolation; it widens citations to
  globals only, which are the only hits employees can retrieve.
- **No write/list surface** for employees: all management endpoints are admin-only (403).
- **Conversations remain per-employee** with 404-on-foreign (no existence leak,
  `chat_service.py:548-557`).

How to test (must-pass):
1. **Retrieval isolation** — seed: global doc G, employee A's (legacy) private doc PA, other
   employee B's private doc PB. As A, `POST /chat/query` for a fact that exists ONLY in PA → answer
   must NOT contain it and `sources` must NOT reference PA. Same for content only in PB.
2. **Global grounding works** — a fact present only in G → answer grounded on G and `sources`
   lists G (validates the `_build_sources` change). Run for **both** `/chat/query` and `/chat/stream`
   (parse the SSE `done` frame's `sources`).
3. **SQL-level** — assert that with `owner_id=None, include_global=True` the store returns only
   `is_global` chunks (unit test over `pgvector_store.query`/`keyword_query`), and that
   `owner_id=None, include_global=False` returns `[]` (default-deny).
4. **Upload denied** — employee token → `POST /documents/upload` returns **403**; same for `GET
   /documents`, `GET/DELETE /documents/{id}`, `POST /documents/{id}/reindex`.
5. **File serve** — employee can `GET /documents/{G}/file` (200) but `GET /documents/{PB}/file`
   → **404** (no existence leak).
6. **No client-flag escape** — confirm the client cannot influence scope: any attempt to pass an
   `owner_id`/`include_global` in the request body is ignored (the server uses `retrieval_scope`
   only).
7. **Regression** — admin retains own+global retrieval and full management; `ALLOW_REGISTRATION`
   is `False` so no self-registered `user` accounts exist outside SSO.

### risks (leakage vectors to watch)

- **Forgetting `/search`.** If `/search` keeps `owner_id=current_user.id` (`search.py:47`) it
  becomes a side channel that returns personal docs even after chat is locked down. Must be
  scoped via `retrieval_scope` or made admin-only.
- **A second retrieval caller added later** that hardcodes `include_global=True` /
  `owner_id=current_user.id` reintroduces the blend. Mitigation: route ALL retrieval through
  `retrieval_scope`; consider asserting in `search_service.search` that authenticated employee
  calls don't pass a non-null `owner_id`.
- **`_build_sources` over-widening.** If `show_global` is enabled for a scope where personal hits
  CAN appear (admin), private excerpts could surface. Guard: `show_global=True` only when
  `owner_id is None` (true global-only mode); never combine `show_global` with a personal scope.
- **`/documents/{id}/file` scope drift.** If the file route is left on `_owner_scope`
  (`documents.py:179`) instead of the global-only scope, an employee could fetch their own legacy
  private file. Low impact once uploads are blocked (they own nothing new), but fix the scope.
- **Legacy/backfilled docs with `owner_id=NULL, is_global=False`** are invisible to employees
  (the clause requires `is_global IS TRUE`) — correct, but ensure intended-shared docs are truly
  flagged `is_global=True`, or they will never ground employee answers.
- **JIT-provisioned employees defaulting to `role="admin"`.** The SSO/JIT path must mint
  `role="user"`; an employee accidentally provisioned as admin bypasses every guard above.
- **Quota-induced silent fallback.** Chat-key exhaustion degrades to the local extractive
  provider (`chat_service.py:338-385`); not a leak, but a quality/availability risk under many
  employees with no rate limiting today.
- **Half-migrated index.** If global chunks aren't (re)embedded with the same provider/dim as the
  query embedder, employees get empty semantic answers — a correctness leak of *availability*.
