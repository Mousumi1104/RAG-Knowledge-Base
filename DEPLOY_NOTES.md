# Deploy Notes — RAG Knowledge Base (Render + Vercel)

Production deployment of the monorepo: **backend → Render** (Docker web service +
managed Postgres with pgvector), **frontend → Vercel** (Next.js).

> Secrets below are listed by **NAME ONLY**. Never put real values in any
> committed file. Set them in the Render dashboard / Vercel project settings.

---

## Render

- **Web service name:** `rag-knowledge-base-api`
  - env: docker, rootDir: `backend`, dockerfilePath: `./Dockerfile`
  - healthCheckPath: `/health`, autoDeploy: true, plan: starter
  - binds Render's injected `$PORT` (see `backend/entrypoint.sh`:
    `uvicorn --host 0.0.0.0 --port "${PORT:-8000}"`)
- **Database name:** `rag-knowledge-base-db` (Postgres major version 16)
- **Persistent disk:** mount a 1 GB disk at `/app/uploads` so uploaded source
  documents (PDF/DOCX/TXT/MD) survive restarts and redeploys. Without it,
  uploaded files are lost on each deploy because the container FS is ephemeral.

Blueprint: see `render.yaml` at repo root.

### Backend env vars

Non-secret (committed in `render.yaml`):

| Name | Value |
|------|-------|
| `EMBEDDING_PROVIDER` | `local` |
| `EMBEDDING_DIM` | `384` |
| `VECTOR_DB` | `pgvector` |
| `LLM_PROVIDER` | `gemini` |
| `GEMINI_MODEL` | `gemini-2.5-flash` |
| `JWT_ALGORITHM` | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `720` |
| `TOP_K` | `8` |
| `CHUNK_SIZE` | `500` |
| `CHUNK_OVERLAP` | `100` |
| `MAX_UPLOAD_MB` | `0` |
| `ALLOWED_EXTENSIONS` | `pdf,docx,txt,md` |
| `LOG_LEVEL` | `INFO` |

Secrets — set in dashboard / API (`sync: false`, names only):

- `DATABASE_URL` — connection string of `rag-knowledge-base-db`
  (use the `postgresql+psycopg://...` SQLAlchemy form the app expects)
- `GEMINI_API_KEY`
- `JWT_SECRET_KEY` — strong random, e.g. `openssl rand -hex 32`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `CORS_ORIGINS` — set to the deployed Vercel frontend origin (see CORS step)

---

## Vercel

- **Root Directory:** `frontend` (the Next.js app lives there; set this on the
  Vercel project — `vercel.json` at repo root only pins `framework: nextjs`).
- **Build-time env var:** `NEXT_PUBLIC_API_BASE_URL`
  - `NEXT_PUBLIC_*` is inlined at **build time** by Next.js, so it must be set
    in the Vercel project **before** the build and a redeploy is required to
    change it. Set it to the public Render API URL, e.g.
    `https://rag-knowledge-base-api.onrender.com`.

---

## Deploy order

1. **Push** the repo (verify `.gitignore` excludes all real `.env` files first).
2. **Provision DB** — create `rag-knowledge-base-db` (Postgres 16). Capture its
   connection string for `DATABASE_URL`.
3. **Backend** — create/deploy `rag-knowledge-base-api` on Render with the env
   vars above. On boot it runs `alembic upgrade head` (enables pgvector +
   creates tables/HNSW index) then starts uvicorn. Confirm `/health` is green.
4. **Frontend** — deploy on Vercel with root `frontend` and
   `NEXT_PUBLIC_API_BASE_URL` pointing at the Render API URL.
5. **CORS** — set the backend `CORS_ORIGINS` to the Vercel frontend origin and
   redeploy the backend so the browser can call the API.
6. **Verify** — load the frontend, log in as admin, upload a document, and run a
   chat query end-to-end. Confirm uploads persist across a backend redeploy
   (validates the `/app/uploads` persistent disk).

---

## Secret safety

Real secret files (`backend/.env`, `frontend/.env.local`, root `.env`) are
gitignored; only `*.example` templates are committed. Never commit real values.
