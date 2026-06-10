# Admin Auth + Route Split Plan

Single-admin JWT auth, a chat-only user page, and a dedicated `/admin` area for
document management + the admin dashboard. **No code changes are made by this
document — it is the implementation plan.**

## Fixed decisions (constraints)

- One Next.js app. `/` (user) = **chat only**. `/admin` = document management +
  admin dashboard, gated by login.
- Auth = **single hardcoded admin from env**. No `users` table, no DB migration.
- JWT bearer tokens via OAuth2 password flow (`PyJWT`, HS256).
- Public chat + search; only `/admin` UI + document-management APIs + `/admin/*`
  APIs are gated.

---

## 0. Files read (current state)

Backend:
- `backend/app/main.py` — `create_app()` factory; `/health` defined inline;
  routers wired: `documents`, `search`, `chat`, `admin`. **No auth anywhere.**
- `backend/app/core/config.py` — pydantic-settings `Settings`; module-level
  `settings`; `get_settings()` is `lru_cache`-d (relevant to tests).
- `backend/app/api/documents.py` — `APIRouter(prefix="/documents")`; routes:
  `POST /upload`, `GET ""` (list), `GET /{doc_id}`, `DELETE /{doc_id}`,
  `POST /{doc_id}/reindex`. No auth.
- `backend/app/api/admin.py` — `APIRouter(prefix="/admin")`; routes:
  `GET /stats`, `GET /indexing-status`, `POST /reindex`, `GET /health`. No auth.
- `backend/app/api/chat.py` — `APIRouter(tags=["chat"])`; `POST /chat/query`,
  `POST /chat/stream`, conversation CRUD. **Stays public.**
- `backend/app/api/search.py` — `APIRouter(tags=["search"])`; `POST /search`.
  **Stays public.**
- `backend/requirements.txt` — no `PyJWT` yet.
- `backend/.env` / `backend/.env.example` — no auth keys yet.
- `backend/tests/conftest.py` — sets env **before** importing app, clears
  `get_settings` cache, rebinds `settings`. Fixtures: `client` (TestClient),
  `db`, autouse schema + truncation. Helper `upload_file(client, ...)` POSTs to
  `/documents/upload`.
- Tests hitting protected endpoints (will break without an authed client):
  `test_documents.py`, `test_admin.py`, `test_chat.py`, `test_chat_stream.py`,
  `test_indexing.py`, `test_search.py` (all call `upload_file` ->
  `POST /documents/upload`). `test_health.py` and `test_models.py` do not.
  Note: `/admin/health` is **protected**; the public liveness probe is `/health`
  (in `main.py`).

Frontend:
- `frontend/app/page.tsx` — renders `<DocumentUpload/>`, `<ChatInterface/>`,
  `<SearchResults/>` (header included). Must become chat-only.
- `frontend/app/layout.tsx` — root layout, no nav.
- `frontend/components/DocumentUpload.tsx` — upload + list + delete + reindex
  (uses `uploadDocument`, `listDocuments`, `deleteDocument`, `reindexDocument`).
  Moves to `/admin`.
- `frontend/components/SearchResults.tsx` — standalone search UI. **Removed from
  the user page** (chat retrieves internally; per fixed decisions it is dropped,
  not moved).
- `frontend/components/ChatInterface.tsx` / `Sources.tsx` — stay on `/`.
- `frontend/services/api.ts` — `API_BASE_URL`; raw `fetch` per call, no auth
  header, no token storage. Needs login + authed fetch helper.

---

## 1. Public / Protected route matrix

| Method | Path | Access |
|---|---|---|
| GET | `/health` | **PUBLIC** (liveness probe in `main.py`) |
| POST | `/auth/login` | **PUBLIC** (issues token) |
| GET | `/auth/me` | **PROTECTED** (admin JWT) |
| POST | `/chat/query` | **PUBLIC** |
| POST | `/chat/stream` | **PUBLIC** |
| GET | `/chat/conversations` | **PUBLIC** |
| GET | `/chat/conversations/{id}` | **PUBLIC** |
| DELETE | `/chat/conversations/{id}` | **PUBLIC** |
| POST | `/search` | **PUBLIC** |
| POST | `/documents/upload` | **PROTECTED** |
| GET | `/documents` | **PROTECTED** |
| GET | `/documents/{id}` | **PROTECTED** |
| DELETE | `/documents/{id}` | **PROTECTED** |
| POST | `/documents/{id}/reindex` | **PROTECTED** |
| GET | `/admin/stats` | **PROTECTED** |
| GET | `/admin/indexing-status` | **PROTECTED** |
| POST | `/admin/reindex` | **PROTECTED** |
| GET | `/admin/health` | **PROTECTED** |

Rule: protect **all** of `/documents*` and **all** of `/admin/*` plus
`GET /auth/me`. Everything else stays public.

---

## 2. Backend implementation

### 2.1 `backend/app/core/config.py` (modify)

Add to `Settings`:

```python
# --- Auth (single hardcoded admin; no users table) -------------------
ADMIN_USERNAME: str = "admin"
ADMIN_PASSWORD: str = "admin123"          # dev default; OVERRIDE IN PROD
JWT_SECRET_KEY: str = "dev-insecure-change-me"  # dev default; OVERRIDE IN PROD
JWT_ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = 720    # 12 hours
```

Notes: dev defaults let tests run with **zero extra env**. Keep the existing
`extra="ignore"` config. No validator needed (all scalars).

### 2.2 `backend/app/core/security.py` (NEW)

Responsibilities (PyJWT, HS256):

- `oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")` — note the
  **relative** `tokenUrl` (no leading slash) so it resolves correctly under any
  mount; it must match the actual login path `/auth/login`.
- `create_access_token(subject: str) -> str` — payload `{"sub": subject,
  "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)}`,
  `jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)`.
- `decode_access_token(token: str) -> dict` — `jwt.decode(..., algorithms=[...])`;
  on `jwt.PyJWTError` raise the 401 credentials exception.
- `authenticate_admin(username, password) -> bool` — compare with
  `secrets.compare_digest` against `settings.ADMIN_USERNAME` /
  `settings.ADMIN_PASSWORD` (constant-time, both fields).
- `get_current_admin(token = Depends(oauth2_scheme)) -> str` — decode, verify
  `sub == settings.ADMIN_USERNAME`, return the username; otherwise raise
  `HTTPException(status_code=401, detail="Could not validate credentials",
  headers={"WWW-Authenticate": "Bearer"})`.
- Read settings **dynamically** inside functions (e.g. `from app.core.config
  import settings` referenced at call time, or `get_settings()`), so the
  conftest cache-clear/rebind is honored at runtime.

### 2.3 `backend/app/api/auth.py` (NEW)

```python
router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login")                       # PUBLIC
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    if not authenticate_admin(form_data.username, form_data.password):
        raise HTTPException(401, "Incorrect username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    return {"access_token": create_access_token(form_data.username),
            "token_type": "bearer"}

@router.get("/me")                           # PROTECTED
def me(admin: str = Depends(get_current_admin)):
    return {"username": admin}
```

`OAuth2PasswordRequestForm` requires **form-encoded** input (`username`,
`password`). Requires `python-multipart` (already in requirements).

### 2.4 `backend/app/api/documents.py` (modify)

Apply auth at the **router level** so every current and future route is covered:

```python
from app.core.security import get_current_admin
router = APIRouter(prefix="/documents", tags=["documents"],
                   dependencies=[Depends(get_current_admin)])
```

No per-route changes needed.

### 2.5 `backend/app/api/admin.py` (modify)

Same router-level dependency:

```python
router = APIRouter(prefix="/admin", tags=["admin"],
                   dependencies=[Depends(get_current_admin)])
```

### 2.6 `backend/app/main.py` (modify)

Import and wire the auth router (public `/auth/login`, protected `/auth/me`):

```python
from app.api import admin, auth, chat, documents, search
...
app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(search.router)
app.include_router(chat.router)
app.include_router(admin.router)
```

`/health` stays inline and public. Leave `chat`/`search` untouched (public).

### 2.7 `backend/requirements.txt` (modify)

Add `PyJWT` (e.g. `PyJWT>=2.8`). `python-multipart` already present (needed for
`OAuth2PasswordRequestForm`).

### 2.8 `backend/.env.example` + `backend/.env` (modify)

Append an Auth section:

```
# --- Auth (single admin; CHANGE IN PRODUCTION) -------------------------------
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
JWT_SECRET_KEY=dev-insecure-change-me   # generate a strong secret in prod
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=720
```

---

## 3. Frontend implementation

### 3.1 `frontend/app/page.tsx` (modify) — chat only

Render only the chat + a nav link to `/admin`. Remove `DocumentUpload` and
`SearchResults` imports/usage.

```tsx
import ChatInterface from "@/components/ChatInterface";
import Link from "next/link";

export default function HomePage() {
  return (
    <main className="container">
      <header className="page-header">
        <h1>RAG Chatbot</h1>
        <p className="muted">Ask questions; answers are grounded in the knowledge base.</p>
        <nav><Link href="/admin">Admin</Link></nav>
      </header>
      <ChatInterface />
    </main>
  );
}
```

### 3.2 `frontend/services/api.ts` (modify)

- `const TOKEN_KEY = "rag_admin_token";`
- `getToken()/setToken()/clearToken()` over `localStorage` (guard `typeof
  window`).
- `login(username, password)`:
  ```ts
  const form = new URLSearchParams();
  form.append("username", username);
  form.append("password", password);
  const res = await fetch(`${API_BASE_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form.toString(),
  });
  // on ok: store access_token; on 401: throw
  ```
- `authedFetch(input, init)` — inject `Authorization: Bearer <token>`; if the
  response is **401**, `clearToken()` (and let the UI redirect to login) before
  throwing.
- Route the **document** calls (`uploadDocument`, `listDocuments`,
  `deleteDocument`, `reindexDocument`) and the new **admin** calls
  (`getAdminStats`, `getIndexingStatus`, `getAdminHealth`, `adminReindex` ->
  `/admin/*`) through `authedFetch`. Keep `search`, `chatQuery`, `streamChat`,
  conversation calls as plain `fetch` (public).
- Add `getMe()` -> `GET /auth/me` (used to validate a stored token on mount).

### 3.3 `frontend/app/admin/page.tsx` (NEW)

- `"use client"`.
- On mount: read token; optionally call `getMe()` to validate; if invalid/none
  -> show **login form** (username/password -> `login()`), then re-render.
- When authed: render
  - `<DocumentUpload/>` (moved here unchanged),
  - `<AdminDashboard/>` (new) showing `/admin/stats`, `/admin/indexing-status`,
    `/admin/health`, with a "Reindex all" button -> `/admin/reindex`,
  - a **Logout** button -> `clearToken()` + back to login,
  - nav `Link` back to `/`.
- 401 from any authed call -> clear token -> bounce to login form.

### 3.4 `frontend/components/AdminDashboard.tsx` (NEW)

Thin client component calling the new authed admin API helpers; renders stats,
indexing status, health, and the reindex-all action.

### 3.5 `frontend/components/DocumentUpload.tsx` (no logic change)

Stays as-is; only its **mount location** moves to `/admin`. Its API calls now go
through `authedFetch` because the `api.ts` functions it imports were updated.

### 3.6 `frontend/components/SearchResults.tsx`

Per fixed decisions, **removed from the user page** and not re-mounted (chat
retrieves internally). The component file can remain unused or be deleted;
recommend leaving it in place to keep the diff small (no longer imported).

### 3.7 `frontend/app/layout.tsx`

No required change. Optionally add a shared top nav, but per-page `Link`s
(3.1 / 3.3) already satisfy "nav links between `/` and `/admin`".

---

## 4. Test impact

Because `/documents*` and `/admin/*` are now protected, every test that uploads
or calls admin will 401 with the current `client` fixture.

### 4.1 `backend/tests/conftest.py` (modify)

- Make the **default `client` authed**: after building the `TestClient`, POST
  form-encoded creds to `/auth/login` (default dev creds
  `admin`/`admin123`, which the dev config defaults provide — **no extra env
  needed**), read `access_token`, and set
  `test_client.headers["Authorization"] = f"Bearer {token}"`. All existing
  upload/admin tests then pass unchanged.
- Add an **`anon_client`** fixture: a plain `TestClient` with no auth header,
  for 401 assertions.
- Keep the env-before-import ordering. The auth settings use defaults, so no new
  `os.environ` entries are strictly required; optionally set
  `ADMIN_USERNAME`/`ADMIN_PASSWORD`/`JWT_SECRET_KEY` explicitly for determinism.
- `upload_file(client, ...)` keeps working since it now receives the authed
  client (it already takes the client as an arg).

### 4.2 `backend/tests/test_auth.py` (NEW)

Cases:
- `login` with correct creds -> 200, body has `access_token` + `token_type ==
  "bearer"`.
- `login` with bad creds -> **401** (+ `WWW-Authenticate: Bearer`).
- `login` with JSON body (wrong content type) -> **422** (form required).
- protected route without token (`anon_client` GET `/documents`) -> **401**.
- protected route with token (authed `client` GET `/documents`) -> **200**.
- `GET /auth/me` with token -> 200 `{username: "admin"}`; without token -> 401.
- public still public on `anon_client`: `GET /health` -> 200; `POST /chat/query`
  and `POST /search` -> not 401 (200/422 by payload validity).

### 4.3 Existing tests — expected outcome

`test_documents`, `test_admin`, `test_chat`, `test_chat_stream`,
`test_indexing`, `test_search` pass unchanged once `client` is authed.
`test_health`, `test_models` are unaffected.

---

## 5. Risks & gotchas

- **Form vs JSON**: `OAuth2PasswordRequestForm` is **form-encoded only**. Sending
  JSON to `/auth/login` -> **422**, not 401. Frontend must use
  `application/x-www-form-urlencoded` (3.2).
- **401 vs 422**: bad credentials = **401**; malformed/missing fields = **422**.
  Tests assert both distinctly (4.2).
- **Dev defaults are insecure**: `ADMIN_PASSWORD=admin123` and a static
  `JWT_SECRET_KEY` MUST be overridden in production. Call this out in
  `.env.example` comments.
- **Token in `localStorage`**: readable by JS -> XSS risk. Acceptable for this
  POC; documented caveat. (Alternative: httpOnly cookie — out of scope here.)
- **Keep chat/search public**: do **not** add the dependency to `chat.py` /
  `search.py`; verify via the public-route tests (4.2).
- **`tokenUrl` correctness**: `OAuth2PasswordBearer(tokenUrl="auth/login")` is
  only metadata for docs/clients but should match the real path; the actual
  login route is `POST /auth/login`.
- **Settings cache in tests**: security code must read `settings` at call time
  (not bind a stale module value at import) so conftest's cache-clear/rebind is
  respected.
- **CORS**: `Authorization` is covered by existing `allow_headers=["*"]`; no
  change needed.

---

## 6. TODO checklist (item -> file targets)

| # | Item | Files |
|---|---|---|
| B1 | Add auth settings | `backend/app/core/config.py` |
| B2 | Security module (PyJWT, OAuth2, `get_current_admin`) | `backend/app/core/security.py` (new) |
| B3 | Auth router (`/auth/login`, `/auth/me`) | `backend/app/api/auth.py` (new) |
| B4 | Router-level auth on documents | `backend/app/api/documents.py` |
| B5 | Router-level auth on admin | `backend/app/api/admin.py` |
| B6 | Wire auth router | `backend/app/main.py` |
| B7 | Add `PyJWT` | `backend/requirements.txt` |
| B8 | Env additions | `backend/.env.example`, `backend/.env` |
| F1 | Chat-only user page + nav | `frontend/app/page.tsx` |
| F2 | `login`, token storage, `authedFetch`, admin API helpers, `getMe` | `frontend/services/api.ts` |
| F3 | `/admin` page: login gate + Logout + nav | `frontend/app/admin/page.tsx` (new) |
| F4 | Admin dashboard component | `frontend/components/AdminDashboard.tsx` (new) |
| F5 | DocumentUpload moved to `/admin` (location only) | `frontend/components/DocumentUpload.tsx` |
| F6 | Drop SearchResults from user page | `frontend/app/page.tsx` (remove import) |
| T1 | Authed default `client` + `anon_client` | `backend/tests/conftest.py` |
| T2 | Auth test suite | `backend/tests/test_auth.py` (new) |
