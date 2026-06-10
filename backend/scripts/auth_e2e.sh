#!/usr/bin/env bash
#
# auth_e2e.sh — Live end-to-end test of the auth boundary (JWT) over the API.
#
# Boots the FastAPI app against the live local PostgreSQL (pgvector) with local
# providers and verifies the public/protected boundary end-to-end with curl:
#   public health + chat (no token) -> 401 on protected routes (no token) ->
#   bad login 401 -> good login 200 (token) -> authed upload + index + stats + me.
#
# Each check prints [auth-e2e] PASS/FAIL; checks are NON-fatal so the run
# continues and a final SUMMARY line reports counts. Exit 0 only when every
# check passed.
#
# Contract sources (read from routers + pydantic schemas + core/security.py):
#   - login                = POST /auth/login form-urlencoded (username,password)
#                            -> 200 {"access_token","token_type"}; bad creds -> 401
#   - me                   = GET /auth/me (Bearer) -> 200 {"username":"admin"}
#   - public               = GET /health -> 200; POST /chat/query {"query":...} -> 200 {"answer":...}
#   - protected            = POST /documents/upload, GET /admin/stats require admin JWT (401 without)
#   - upload multipart     = field "file" (POST /documents/upload -> 201, DocumentOut)
#   - indexed status field = indexing_status == "indexed" (DocumentOut / GET /documents)
#   - stats                = GET /admin/stats -> 200 {"total_documents", ...} (StatsResponse)
#   - default admin creds  = admin / admin123 (app.core.config defaults)
#
set -euo pipefail

# --- Resolve repo root from this script's own dir ---------------------------
SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

# --- Config -----------------------------------------------------------------
HOST="127.0.0.1"
PORT="8000"
BASE="http://${HOST}:${PORT}"
DATABASE_URL="postgresql+psycopg://raguser@127.0.0.1:54329/ragdb"
export DATABASE_URL
export VECTOR_DB="pgvector"
export EMBEDDING_PROVIDER="local"
export LLM_PROVIDER="local"

ADMIN_USER="admin"
ADMIN_PASS="admin123"

UVICORN_LOG="$(mktemp -t auth_e2e_uvicorn.XXXXXX.log)"
TMPFILE="$(mktemp -t auth_e2e_doc.XXXXXX.txt)"
TMPBODY="$(mktemp -t auth_e2e_body.XXXXXX.json)"
PID=""

PASS_COUNT=0
FAIL_COUNT=0

# --- Logging helpers --------------------------------------------------------
# Non-fatal: failures increment the counter and continue so we get a full SUMMARY.
pass() { echo "[auth-e2e] PASS: $*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "[auth-e2e] FAIL: $*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }
info() { echo "[auth-e2e] $*"; }

# Fatal: only for setup failures that make the rest of the run meaningless.
die() {
  echo "[auth-e2e] FATAL: $*" >&2
  FAIL_COUNT=$((FAIL_COUNT + 1))
  echo "[auth-e2e] ----- uvicorn log (tail) -----" >&2
  tail -n 60 "$UVICORN_LOG" >&2 2>/dev/null || true
  echo "[auth-e2e] ------------------------------" >&2
  summary
  exit 1
}

summary() {
  echo
  echo "[auth-e2e] =============================================="
  echo "[auth-e2e] SUMMARY: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
  echo "[auth-e2e] =============================================="
}

# --- Cleanup ----------------------------------------------------------------
cleanup() {
  if [[ -n "${PID:-}" ]]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -f "$TMPFILE" "$UVICORN_LOG" "$TMPBODY" 2>/dev/null || true
}
trap cleanup EXIT

# --- jq detection -----------------------------------------------------------
HAVE_JQ=0
if command -v jq >/dev/null 2>&1; then
  HAVE_JQ=1
fi

# --- Helpers ----------------------------------------------------------------
# HTTP status code only (body discarded). Args: METHOD URL [extra curl args...]
http_code() {
  local method="$1" url="$2"; shift 2
  curl -s -o /dev/null -w '%{http_code}' -X "$method" "$@" "$url" 2>/dev/null || echo 000
}

# ===========================================================================
# Step 1: Ensure PostgreSQL is up + apply Alembic migrations + start server
# ===========================================================================
info "Ensuring local PostgreSQL is up ..."
if bash "$REPO_ROOT/backend/scripts/pg_local.sh" up; then
  pass "PostgreSQL is up"
else
  die "PostgreSQL failed to start"
fi

info "Applying Alembic migrations (upgrade head) ..."
if (cd "$BACKEND_DIR" && DATABASE_URL="$DATABASE_URL" ./venv/bin/alembic upgrade head); then
  pass "Alembic upgrade head"
else
  die "Alembic upgrade head failed"
fi

info "Starting uvicorn ..."
(
  cd "$BACKEND_DIR" && \
  DATABASE_URL="$DATABASE_URL" \
  VECTOR_DB="pgvector" \
  EMBEDDING_PROVIDER="local" \
  LLM_PROVIDER="local" \
  ./venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
) >"$UVICORN_LOG" 2>&1 &
PID=$!
info "uvicorn PID=$PID (log: $UVICORN_LOG)"

info "Waiting for /health ..."
HEALTH_OK=0
for _ in $(seq 1 80); do
  if ! kill -0 "$PID" 2>/dev/null; then
    die "uvicorn process died during startup"
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/health" 2>/dev/null || echo 000)"
  if [[ "$code" == "200" ]]; then
    HEALTH_OK=1
    break
  fi
  sleep 0.5
done
if [[ "$HEALTH_OK" == "1" ]]; then
  pass "Server healthy (GET /health -> 200)"
else
  die "Server did not become healthy within timeout"
fi

# ===========================================================================
# Step 2: PUBLIC — /health 200 and /chat/query (no token) 200 with an answer
# ===========================================================================
info "PUBLIC: GET /health (no token) ..."
code="$(http_code GET "${BASE}/health")"
if [[ "$code" == "200" ]]; then
  pass "GET /health (no token) -> 200"
else
  fail "GET /health (no token) -> $code (expected 200)"
fi

info "PUBLIC: POST /chat/query (no token) ..."
code="$(curl -s -o "$TMPBODY" -w '%{http_code}' -X POST "${BASE}/chat/query" \
  -H 'Content-Type: application/json' \
  -d '{"query":"hello"}' 2>/dev/null || echo 000)"
if [[ "$HAVE_JQ" == "1" ]]; then
  ANSWER="$(jq -r 'has("answer")' "$TMPBODY" 2>/dev/null || echo false)"
else
  if grep -qE '"answer"[[:space:]]*:' "$TMPBODY" 2>/dev/null; then ANSWER="true"; else ANSWER="false"; fi
fi
if [[ "$code" == "200" && "$ANSWER" == "true" ]]; then
  pass "POST /chat/query (no token) -> 200 with answer"
else
  echo "[auth-e2e] chat body: $(cat "$TMPBODY" 2>/dev/null)" >&2
  fail "POST /chat/query (no token) -> $code (expected 200 with answer; has_answer=$ANSWER)"
fi

# ===========================================================================
# Step 3: DENIED (no token) — protected routes must 401
# ===========================================================================
info "DENIED: POST /documents/upload (no token) ..."
printf 'auth boundary probe document\n' >"$TMPFILE"
code="$(http_code POST "${BASE}/documents/upload" -F "file=@${TMPFILE};type=text/plain")"
if [[ "$code" == "401" ]]; then
  pass "POST /documents/upload (no token) -> 401"
else
  fail "POST /documents/upload (no token) -> $code (expected 401)"
fi

info "DENIED: GET /admin/stats (no token) ..."
code="$(http_code GET "${BASE}/admin/stats")"
if [[ "$code" == "401" ]]; then
  pass "GET /admin/stats (no token) -> 401"
else
  fail "GET /admin/stats (no token) -> $code (expected 401)"
fi

# ===========================================================================
# Step 4: BAD LOGIN — wrong password must 401
# ===========================================================================
info "BAD LOGIN: POST /auth/login with wrong password ..."
code="$(http_code POST "${BASE}/auth/login" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=${ADMIN_USER}" \
  --data-urlencode "password=wrong-password")"
if [[ "$code" == "401" ]]; then
  pass "POST /auth/login (bad password) -> 401"
else
  fail "POST /auth/login (bad password) -> $code (expected 401)"
fi

# ===========================================================================
# Step 5: LOGIN — good creds -> 200, extract access_token
# ===========================================================================
info "LOGIN: POST /auth/login with valid admin creds ..."
code="$(curl -s -o "$TMPBODY" -w '%{http_code}' -X POST "${BASE}/auth/login" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d "username=${ADMIN_USER}&password=${ADMIN_PASS}" 2>/dev/null || echo 000)"
TOKEN=""
if [[ "$HAVE_JQ" == "1" ]]; then
  TOKEN="$(jq -r '.access_token // empty' "$TMPBODY" 2>/dev/null || true)"
else
  TOKEN="$(grep -oE '"access_token"[[:space:]]*:[[:space:]]*"[^"]+"' "$TMPBODY" \
    | head -n1 | sed -E 's/.*:[[:space:]]*"([^"]+)"/\1/' || true)"
fi
if [[ "$code" == "200" && -n "${TOKEN:-}" ]]; then
  pass "POST /auth/login (good creds) -> 200, token acquired"
else
  echo "[auth-e2e] login body: $(cat "$TMPBODY" 2>/dev/null)" >&2
  fail "POST /auth/login (good creds) -> $code (expected 200 with access_token)"
fi

AUTH_HDR="Authorization: Bearer ${TOKEN:-}"

# Guard: the authed steps are meaningless without a token.
if [[ -z "${TOKEN:-}" ]]; then
  fail "No token available; skipping authed checks (upload/index/stats/me)"
  summary
  if [[ "$FAIL_COUNT" -eq 0 ]]; then exit 0; else exit 1; fi
fi

# ===========================================================================
# Step 6: ALLOWED (with Bearer) — upload -> indexed, stats, me
# ===========================================================================
info "ALLOWED: POST /documents/upload (with token) ..."
code="$(curl -s -o "$TMPBODY" -w '%{http_code}' \
  -H "$AUTH_HDR" \
  -F "file=@${TMPFILE};type=text/plain" \
  "${BASE}/documents/upload" 2>/dev/null || echo 000)"
DOC_ID=""
if [[ "$HAVE_JQ" == "1" ]]; then
  DOC_ID="$(jq -r '.id // empty' "$TMPBODY" 2>/dev/null || true)"
else
  DOC_ID="$(grep -oE '"id"[[:space:]]*:[[:space:]]*[0-9]+' "$TMPBODY" \
    | head -n1 | grep -oE '[0-9]+' || true)"
fi
if [[ "$code" == "201" && -n "${DOC_ID:-}" ]]; then
  pass "POST /documents/upload (with token) -> 201 (id=$DOC_ID)"
else
  echo "[auth-e2e] upload body: $(cat "$TMPBODY" 2>/dev/null)" >&2
  fail "POST /documents/upload (with token) -> $code (expected 201 with id)"
fi

if [[ -n "${DOC_ID:-}" ]]; then
  info "Waiting for document $DOC_ID to be indexed (GET /documents with token) ..."
  INDEXED_OK=0
  STATUS="unknown"
  for _ in $(seq 1 60); do
    DOCS_BODY="$(curl -s -H "$AUTH_HDR" "${BASE}/documents" 2>/dev/null || true)"
    if [[ "$HAVE_JQ" == "1" ]]; then
      STATUS="$(printf '%s' "$DOCS_BODY" | jq -r --argjson id "$DOC_ID" \
        '.items[] | select(.id == $id) | .indexing_status' 2>/dev/null | head -n1 || true)"
    else
      if printf '%s' "$DOCS_BODY" | grep -qE "\"id\"[[:space:]]*:[[:space:]]*${DOC_ID}\b"; then
        if printf '%s' "$DOCS_BODY" | grep -qE '"indexing_status"[[:space:]]*:[[:space:]]*"indexed"'; then
          STATUS="indexed"
        elif printf '%s' "$DOCS_BODY" | grep -qE '"indexing_status"[[:space:]]*:[[:space:]]*"failed"'; then
          STATUS="failed"
        else
          STATUS="pending"
        fi
      fi
    fi
    if [[ "$STATUS" == "indexed" ]]; then INDEXED_OK=1; break; fi
    if [[ "$STATUS" == "failed" ]]; then break; fi
    sleep 0.5
  done
  if [[ "$INDEXED_OK" == "1" ]]; then
    pass "Document indexed (GET /documents with token shows indexing_status=indexed)"
  else
    fail "Document did not reach indexing_status=indexed (last status=${STATUS:-unknown})"
  fi
else
  fail "Skipping index wait; no document id from upload"
fi

info "ALLOWED: GET /admin/stats (with token) ..."
code="$(curl -s -o "$TMPBODY" -w '%{http_code}' -H "$AUTH_HDR" "${BASE}/admin/stats" 2>/dev/null || echo 000)"
TOTAL_DOCS=""
if [[ "$HAVE_JQ" == "1" ]]; then
  TOTAL_DOCS="$(jq -r '.total_documents // empty' "$TMPBODY" 2>/dev/null || true)"
else
  TOTAL_DOCS="$(grep -oE '"total_documents"[[:space:]]*:[[:space:]]*[0-9]+' "$TMPBODY" \
    | head -n1 | grep -oE '[0-9]+' || true)"
fi
if [[ "$code" == "200" && "${TOTAL_DOCS:-0}" =~ ^[0-9]+$ && "${TOTAL_DOCS:-0}" -ge 1 ]]; then
  pass "GET /admin/stats (with token) -> 200 (total_documents=$TOTAL_DOCS)"
else
  echo "[auth-e2e] stats body: $(cat "$TMPBODY" 2>/dev/null)" >&2
  fail "GET /admin/stats (with token) -> $code (expected 200 with total_documents>=1; got=${TOTAL_DOCS:-none})"
fi

info "ALLOWED: GET /auth/me (with token) ..."
code="$(curl -s -o "$TMPBODY" -w '%{http_code}' -H "$AUTH_HDR" "${BASE}/auth/me" 2>/dev/null || echo 000)"
ME_USER=""
if [[ "$HAVE_JQ" == "1" ]]; then
  ME_USER="$(jq -r '.username // empty' "$TMPBODY" 2>/dev/null || true)"
else
  ME_USER="$(grep -oE '"username"[[:space:]]*:[[:space:]]*"[^"]+"' "$TMPBODY" \
    | head -n1 | sed -E 's/.*:[[:space:]]*"([^"]+)"/\1/' || true)"
fi
if [[ "$code" == "200" && "$ME_USER" == "$ADMIN_USER" ]]; then
  pass "GET /auth/me (with token) -> 200 (username=$ME_USER)"
else
  echo "[auth-e2e] me body: $(cat "$TMPBODY" 2>/dev/null)" >&2
  fail "GET /auth/me (with token) -> $code (expected 200 username=$ADMIN_USER; got=${ME_USER:-none})"
fi

# ===========================================================================
# Summary
# ===========================================================================
summary

if [[ "$FAIL_COUNT" -eq 0 ]]; then
  echo "[auth-e2e] ALL CHECKS PASSED"
  exit 0
else
  echo "[auth-e2e] SOME CHECKS FAILED" >&2
  exit 1
fi
