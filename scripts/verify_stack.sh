#!/usr/bin/env bash
#
# verify_stack.sh — Full-stack verification for the RAG Chatbot.
#
# Proves all services run and are wired together end to end:
#   1. Local Postgres+pgvector is up.
#   2. Alembic migrations apply (upgrade head) against ragdb.
#   3. Backend (uvicorn) boots with the local env and serves GET /health 200.
#   4. CORS: backend echoes access-control-allow-origin for the frontend origin.
#   5. Data path: backend/scripts/e2e_smoke.sh proves upload->index->search->chat
#      (run AFTER stopping our server to avoid a port 8000 conflict).
#   6. Frontend `npm run build` compiles (TS API client + pages).
#
# Logs [verify] PASS/FAIL per step, cleans up background servers + temp files via
# an EXIT trap, and exits non-zero if anything failed.
#
set -euo pipefail

# --- Resolve repo root from this script's own dir ---------------------------
SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"

# --- Config -----------------------------------------------------------------
HOST="127.0.0.1"
PORT="8000"
BASE="http://${HOST}:${PORT}"
FRONTEND_ORIGIN="http://localhost:3000"
APP_DB_URL="postgresql+psycopg://raguser@127.0.0.1:54329/ragdb"

UVICORN_LOG="$(mktemp -t verify_uvicorn.XXXXXX.log)"
PID=""

PASS_COUNT=0
FAIL_COUNT=0

# --- Logging helpers --------------------------------------------------------
pass() { echo "[verify] PASS: $*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "[verify] FAIL: $*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }
info() { echo "[verify] $*"; }

# --- Cleanup ----------------------------------------------------------------
stop_server() {
  if [[ -n "${PID:-}" ]]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
    PID=""
  fi
}

cleanup() {
  stop_server
  rm -f "$UVICORN_LOG" 2>/dev/null || true
}
trap cleanup EXIT

dump_uvicorn_log() {
  echo "[verify] ----- uvicorn log (tail) -----" >&2
  tail -n 60 "$UVICORN_LOG" >&2 2>/dev/null || true
  echo "[verify] ------------------------------" >&2
}

# ===========================================================================
# Step 1: Ensure local PostgreSQL (pgvector) is up
# ===========================================================================
info "Step 1: starting local PostgreSQL ..."
if bash "$BACKEND_DIR/scripts/pg_local.sh" up; then
  pass "PostgreSQL is up (127.0.0.1:54329)"
else
  fail "PostgreSQL failed to start"
fi

# ===========================================================================
# Step 2: Apply Alembic migrations (idempotent)
# ===========================================================================
info "Step 2: applying Alembic migrations (upgrade head) ..."
if (cd "$BACKEND_DIR" && DATABASE_URL="$APP_DB_URL" ./venv/bin/alembic upgrade head); then
  pass "Alembic migrations applied (upgrade head)"
else
  fail "Alembic upgrade head failed"
fi

# ===========================================================================
# Step 3: Start backend uvicorn in the background; poll /health
# ===========================================================================
info "Step 3: starting backend uvicorn on ${BASE} ..."
(
  cd "$BACKEND_DIR" && \
  DATABASE_URL="$APP_DB_URL" \
  VECTOR_DB="pgvector" \
  EMBEDDING_PROVIDER="local" \
  LLM_PROVIDER="local" \
  ./venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
) >"$UVICORN_LOG" 2>&1 &
PID=$!
info "uvicorn PID=$PID (log: $UVICORN_LOG)"

HEALTH_OK=0
for _ in $(seq 1 80); do
  if ! kill -0 "$PID" 2>/dev/null; then
    info "uvicorn process died during startup"
    break
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/health" 2>/dev/null || echo 000)"
  if [[ "$code" == "200" ]]; then
    HEALTH_OK=1
    break
  fi
  sleep 0.5
done

if [[ "$HEALTH_OK" == "1" ]]; then
  pass "Backend healthy (GET /health -> 200)"
else
  fail "Backend did not become healthy within ~40s"
  dump_uvicorn_log
fi

# ===========================================================================
# Step 4: CORS connectivity for the frontend origin
# ===========================================================================
# FastAPI's CORSMiddleware echoes access-control-allow-origin on simple (non
# preflight) GETs when the request Origin is in the allow list. We check /health
# first; if absent there we retry against a real API GET (/documents).
info "Step 4: checking CORS for origin ${FRONTEND_ORIGIN} ..."
if [[ "$HEALTH_OK" == "1" ]]; then
  cors_check() {
    local path="$1"
    curl -s -D- -o /dev/null -H "Origin: ${FRONTEND_ORIGIN}" "${BASE}${path}" 2>/dev/null \
      | grep -i '^access-control-allow-origin:' || true
  }

  CORS_HDR="$(cors_check /health)"
  CORS_PATH="/health"
  if [[ -z "$CORS_HDR" ]]; then
    info "no CORS header on /health; retrying against /documents ..."
    CORS_HDR="$(cors_check /documents)"
    CORS_PATH="/documents"
  fi

  if [[ -n "$CORS_HDR" ]]; then
    pass "CORS header present on ${CORS_PATH} ($(echo "$CORS_HDR" | tr -d '\r'))"
  else
    fail "No access-control-allow-origin header for ${FRONTEND_ORIGIN} (checked /health and /documents)"
  fi
else
  fail "Skipping CORS check (backend not healthy)"
fi

# ===========================================================================
# Step 5: Full data-path proof via e2e_smoke.sh
# ===========================================================================
# e2e_smoke.sh starts its OWN uvicorn on port 8000, so we must stop ours first
# to avoid an address-in-use conflict.
info "Step 5: stopping our server, then running e2e_smoke.sh (data path) ..."
stop_server
if bash "$BACKEND_DIR/scripts/e2e_smoke.sh"; then
  pass "End-to-end data path OK (upload->index->search->chat via e2e_smoke.sh)"
else
  fail "e2e_smoke.sh failed (exit non-zero)"
fi

# ===========================================================================
# Step 6: Frontend production build
# ===========================================================================
info "Step 6: building frontend (npm run build) ..."
if (cd "$FRONTEND_DIR" && npm run build); then
  pass "Frontend build succeeded (npm run build)"
else
  fail "Frontend build failed"
fi

# ===========================================================================
# Summary
# ===========================================================================
echo
echo "[verify] =============================================="
echo "[verify] SUMMARY: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
echo "[verify] =============================================="

if [[ "$FAIL_COUNT" -eq 0 ]]; then
  echo "[verify] ALL CHECKS PASSED"
  exit 0
else
  echo "[verify] SOME CHECKS FAILED" >&2
  exit 1
fi
