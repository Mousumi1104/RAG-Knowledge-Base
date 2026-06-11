# Deployment Result — RAG Knowledge Base

**Status:** ✅ Live and verified (end-to-end checks all passed)
**Deployed:** 2026-06-10

## Live URLs

| Component | URL |
| --- | --- |
| **Frontend (Vercel)** | https://rag-knowledge-base-seven.vercel.app |
| **Backend API (Render)** | https://rag-knowledge-base-86tb.onrender.com |
| Backend health | https://rag-knowledge-base-86tb.onrender.com/health |
| API docs (OpenAPI) | https://rag-knowledge-base-86tb.onrender.com/docs |

## Admin login
- **Username:** `admin`
- **Password:** `3boZjJcdU9UGZx7beJRu`

> Rotate this into a password manager and change it. It was generated during deploy and set as the `ADMIN_PASSWORD` env var on Render.

## Cloud resources & dashboards

| Resource | Identifier | Dashboard |
| --- | --- | --- |
| GitHub repo (private) | `mousumibitpastel-rgb/RAG-Knowledge-Base` | https://github.com/mousumibitpastel-rgb/RAG-Knowledge-Base |
| Render web service | `srv-d8kfursm0tmc73cl64ng` | https://dashboard.render.com/web/srv-d8kfursm0tmc73cl64ng |
| Render Postgres (shared, free) | `dpg-d8eh30740ujc73djdac0-a` (db `yp-custom-db`) | https://dashboard.render.com/d/dpg-d8eh30740ujc73djdac0-a |
| Vercel project | `rag-knowledge-base` (team `mousumibitpastel-rgbs-projects`) | https://vercel.com/mousumibitpastel-rgbs-projects/rag-knowledge-base |

## Configuration

- **Embeddings:** `local` (384-dim) — fixed to match the `chunks.embedding vector(384)` column + HNSW index.
- **Chat LLM:** `gemini` (`gemini-2.5-flash`) via `GEMINI_API_KEY`. Verified returning real answers (not the local fallback).
- **Uploads:** stored on **Cloudinary** (Render free tier has no persistent disk). Backend storage/extraction/serving are URL-aware; serving redirects (307) to the Cloudinary URL.
- **Database:** Render free Postgres with `pgvector`; `alembic upgrade head` runs on every boot (idempotent) — created the extension, tables, and HNSW index.
- **Plan:** Render `free` for both DB and web service; Vercel for the frontend.

## Verification summary (all passed)

| Check | Result |
| --- | --- |
| Backend `/health` | 200 `{"status":"ok"}` |
| Frontend reachable | 200 (Next.js app shell) |
| Admin login → JWT | 200, bearer token returned (OAuth2 password flow) |
| Authenticated `GET /documents` | 200 `{"items":[],"total":0}` |
| CORS preflight from Vercel origin | `access-control-allow-origin` reflects the Vercel origin |
| `POST /chat/query` | 200, clean Gemini answer (empty KB → no sources) |

## Deploy-time fixes applied (committed to the repo)

1. `backend/entrypoint.sh` now binds `--port "${PORT:-8000}"` (Render injects `$PORT`).
2. **Cloudinary storage** integration (`app/utils/cloud_storage.py`, config, document_service, extraction, documents) with a local-disk fallback. (commit `97e20b8`)
3. `response_model=None` on the file-serve route — the `FileResponse | RedirectResponse` union otherwise crashed FastAPI on boot. (commit `2eb6c1c`)
4. `.gitignore` hardened so `backend/.env`, `frontend/.env.local`, and `.a5c/` are never committed.
5. Added `render.yaml`, `vercel.json`, `DEPLOY_NOTES.md`.

## Important notes / next steps

- ⚠️ **Free Postgres expires ~2026-07-01** (~30 days). Upgrade to a paid Render Postgres (or migrate) before then to avoid data loss. It is **shared** with another app (`yp-custom`); alembic tracks its own revision history so the RAG tables are isolated, but they live in the same database.
- ⚠️ **Free web service cold-starts** after ~15 min idle (first request ~30–60s).
- **Populate the knowledge base:** log in as admin and upload documents (PDF/DOCX/TXT/MD) — they index into pgvector and become searchable in chat.
- **Secrets:** `JWT_SECRET_KEY` and `ADMIN_PASSWORD` were generated and set on Render only. Rotate them into a secrets manager. The `.github/workflows/ci.yml` is not on the remote (the deploy PAT lacks `workflow` scope) — re-add it with a `workflow`-scoped token if you want CI.
- **Custom domain (optional):** add one in Vercel; if you do, add that origin to the backend `CORS_ORIGINS` env var and redeploy.
- **Repo visibility:** the repo is private and Render's GitHub App has been granted access to it.
