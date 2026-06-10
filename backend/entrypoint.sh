#!/bin/sh
# Container entrypoint: apply DB migrations, then start the API.
#
# `alembic upgrade head` resolves the DATABASE_URL from the environment
# (see migrations/env.py), so it targets the same DB the app uses. Running it
# on every boot is idempotent — already-applied revisions are skipped — which
# keeps the schema (pgvector extension + tables + HNSW index) in lockstep with
# the code without a separate migration step.
set -e

echo "[entrypoint] Running database migrations (alembic upgrade head)..."
alembic upgrade head

echo "[entrypoint] Starting uvicorn on 0.0.0.0:${PORT:-8000}..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
