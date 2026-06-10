#!/usr/bin/env bash
#
# e2e_smoke.sh — End-to-end smoke test for the RAG Knowledge Base API.
#
# Boots the FastAPI app against the live local PostgreSQL (pgvector) and
# exercises the full RAG flow with curl: upload -> index -> search -> chat ->
# admin stats. Every step asserts real results; the script exits non-zero on
# the first failure (set -euo pipefail + explicit assertions).
#
# Field names / values were taken from the routers + pydantic schemas:
#   - upload multipart field            = "file"  (POST /documents/upload -> 201, DocumentOut)
#   - indexed status field/value        = indexing_status == "indexed"  (DocumentOut.indexing_status)
#   - chunk count field                 = chunk_count
#   - search response                   = {query, results:[{document_id, document_name, excerpt, chunk_id, score}]}
#   - chat response                     = {answer, sources:[{document, excerpt, document_id, score}]}
#   - admin stats fields                = total_documents, indexed
#   - health endpoint                   = GET /health (mounted at root) -> {"status":"ok"}
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

UVICORN_LOG="$(mktemp -t e2e_uvicorn.XXXXXX.log)"
TMPFILE="$(mktemp -t e2e_doc.XXXXXX.txt)"
PID=""

PASS_COUNT=0
FAIL_COUNT=0

# --- Logging helpers --------------------------------------------------------
pass() { echo "[e2e] PASS: $*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() {
  echo "[e2e] FAIL: $*" >&2
  FAIL_COUNT=$((FAIL_COUNT + 1))
  echo "[e2e] ----- uvicorn log (tail) -----" >&2
  tail -n 60 "$UVICORN_LOG" >&2 2>/dev/null || true
  echo "[e2e] ------------------------------" >&2
  exit 1
}
info() { echo "[e2e] $*"; }

# --- Cleanup ----------------------------------------------------------------
cleanup() {
  if [[ -n "${PID:-}" ]]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -f "$TMPFILE" "$UVICORN_LOG" 2>/dev/null || true
}
trap cleanup EXIT

# --- jq detection -----------------------------------------------------------
HAVE_JQ=0
if command -v jq >/dev/null 2>&1; then
  HAVE_JQ=1
fi

# ===========================================================================
# Step 1: Ensure PostgreSQL is up
# ===========================================================================
info "Ensuring local PostgreSQL is up ..."
if bash "$REPO_ROOT/backend/scripts/pg_local.sh" up; then
  pass "PostgreSQL is up"
else
  fail "PostgreSQL failed to start"
fi

# ===========================================================================
# Step 2: Apply Alembic migrations (idempotent)
# ===========================================================================
info "Applying Alembic migrations (upgrade head) ..."
if (cd "$BACKEND_DIR" && DATABASE_URL="$DATABASE_URL" ./venv/bin/alembic upgrade head); then
  pass "Alembic upgrade head"
else
  fail "Alembic upgrade head failed"
fi

# ===========================================================================
# Step 3: Start uvicorn in the background
# ===========================================================================
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

# ===========================================================================
# Step 4: Poll /health until HTTP 200 (timeout ~40s)
# ===========================================================================
info "Waiting for /health ..."
HEALTH_OK=0
for _ in $(seq 1 80); do
  if ! kill -0 "$PID" 2>/dev/null; then
    fail "uvicorn process died during startup"
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
  fail "Server did not become healthy within timeout"
fi

# ===========================================================================
# Step 5: Create a known-content .txt document
# ===========================================================================
cat >"$TMPFILE" <<'EOF'
Employees must submit leave requests through the HR portal for approval. Managers approve within two business days.
EOF
info "Created temp document: $TMPFILE"

# ===========================================================================
# Step 6: Upload the document (POST /documents/upload, field "file") -> 201
# ===========================================================================
info "Uploading document ..."
UPLOAD_BODY="$(mktemp -t e2e_upload.XXXXXX.json)"
upload_code="$(curl -s -o "$UPLOAD_BODY" -w '%{http_code}' \
  -F "file=@${TMPFILE};type=text/plain" \
  "${BASE}/documents/upload" 2>/dev/null || echo 000)"

if [[ "$upload_code" != "200" && "$upload_code" != "201" ]]; then
  echo "[e2e] upload response body:" >&2
  cat "$UPLOAD_BODY" >&2 2>/dev/null || true
  rm -f "$UPLOAD_BODY"
  fail "Upload returned HTTP $upload_code (expected 201/200)"
fi

if [[ "$HAVE_JQ" == "1" ]]; then
  DOC_ID="$(jq -r '.id // empty' "$UPLOAD_BODY")"
else
  DOC_ID="$(grep -oE '"id"[[:space:]]*:[[:space:]]*[0-9]+' "$UPLOAD_BODY" | head -n1 | grep -oE '[0-9]+' || true)"
fi
rm -f "$UPLOAD_BODY"

if [[ -z "${DOC_ID:-}" ]]; then
  fail "Upload succeeded but no document id returned"
fi
pass "Document uploaded (HTTP $upload_code, id=$DOC_ID)"

# ===========================================================================
# Step 7: Poll GET /documents/{id} until indexing_status == "indexed"
# ===========================================================================
info "Waiting for document $DOC_ID to be indexed ..."
INDEXED_OK=0
CHUNK_COUNT="?"
for _ in $(seq 1 60); do
  DOC_BODY="$(curl -s "${BASE}/documents/${DOC_ID}" 2>/dev/null || true)"
  if [[ "$HAVE_JQ" == "1" ]]; then
    STATUS="$(printf '%s' "$DOC_BODY" | jq -r '.indexing_status // empty' 2>/dev/null || true)"
    CHUNK_COUNT="$(printf '%s' "$DOC_BODY" | jq -r '.chunk_count // 0' 2>/dev/null || echo 0)"
  else
    STATUS="$(printf '%s' "$DOC_BODY" | grep -oE '"indexing_status"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/' || true)"
    CHUNK_COUNT="$(printf '%s' "$DOC_BODY" | grep -oE '"chunk_count"[[:space:]]*:[[:space:]]*[0-9]+' | head -n1 | grep -oE '[0-9]+' || echo 0)"
  fi
  if [[ "$STATUS" == "indexed" ]]; then
    INDEXED_OK=1
    break
  fi
  if [[ "$STATUS" == "failed" ]]; then
    echo "[e2e] document body: $DOC_BODY" >&2
    fail "Document indexing failed (indexing_status=failed)"
  fi
  sleep 0.5
done

if [[ "$INDEXED_OK" != "1" ]]; then
  fail "Document did not reach indexing_status=indexed within timeout (last status=${STATUS:-unknown})"
fi
# chunk_count > 0 assertion (numeric, defensive)
if [[ "$CHUNK_COUNT" =~ ^[0-9]+$ ]] && [[ "$CHUNK_COUNT" -gt 0 ]]; then
  pass "Document indexed (indexing_status=indexed, chunk_count=$CHUNK_COUNT)"
else
  fail "Document indexed but chunk_count not > 0 (chunk_count=$CHUNK_COUNT)"
fi

# ===========================================================================
# Step 8: POST /search -> assert >=1 result with non-empty document_name+excerpt
# ===========================================================================
info "Running semantic search ..."
SEARCH_BODY="$(curl -s -X POST "${BASE}/search" \
  -H 'Content-Type: application/json' \
  -d '{"query":"how do I request leave?","top_k":5}' 2>/dev/null || true)"

if [[ "$HAVE_JQ" == "1" ]]; then
  RESULT_COUNT="$(printf '%s' "$SEARCH_BODY" | jq -r '.results | length' 2>/dev/null || echo 0)"
  TOP_NAME="$(printf '%s' "$SEARCH_BODY" | jq -r '.results[0].document_name // empty' 2>/dev/null || true)"
  TOP_EXCERPT="$(printf '%s' "$SEARCH_BODY" | jq -r '.results[0].excerpt // empty' 2>/dev/null || true)"
else
  RESULT_COUNT="$(printf '%s' "$SEARCH_BODY" | grep -oc '"document_id"' || echo 0)"
  TOP_NAME="$(printf '%s' "$SEARCH_BODY" | grep -oE '"document_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/' || true)"
  TOP_EXCERPT="$(printf '%s' "$SEARCH_BODY" | grep -oE '"excerpt"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/' || true)"
fi

if [[ ! "$RESULT_COUNT" =~ ^[0-9]+$ ]] || [[ "$RESULT_COUNT" -lt 1 ]]; then
  echo "[e2e] search body: $SEARCH_BODY" >&2
  fail "Search returned no results"
fi
if [[ -z "$TOP_NAME" ]]; then
  echo "[e2e] search body: $SEARCH_BODY" >&2
  fail "Search top result has empty document_name"
fi
if [[ -z "$TOP_EXCERPT" ]]; then
  echo "[e2e] search body: $SEARCH_BODY" >&2
  fail "Search top result has empty excerpt"
fi
pass "Search returned $RESULT_COUNT result(s); top document_name='$TOP_NAME'"

# ===========================================================================
# Step 9: POST /chat/query -> assert non-empty answer AND non-empty sources[]
# ===========================================================================
info "Running RAG chat query ..."
CHAT_BODY="$(curl -s -X POST "${BASE}/chat/query" \
  -H 'Content-Type: application/json' \
  -d '{"query":"How do employees request leave?"}' 2>/dev/null || true)"

if [[ "$HAVE_JQ" == "1" ]]; then
  ANSWER="$(printf '%s' "$CHAT_BODY" | jq -r '.answer // empty' 2>/dev/null || true)"
  SOURCES_COUNT="$(printf '%s' "$CHAT_BODY" | jq -r '.sources | length' 2>/dev/null || echo 0)"
else
  ANSWER="$(printf '%s' "$CHAT_BODY" | grep -oE '"answer"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/' || true)"
  SOURCES_COUNT="$(printf '%s' "$CHAT_BODY" | grep -oc '"document_id"' || echo 0)"
fi

if [[ -z "$ANSWER" ]]; then
  echo "[e2e] chat body: $CHAT_BODY" >&2
  fail "Chat response has empty answer"
fi
if [[ ! "$SOURCES_COUNT" =~ ^[0-9]+$ ]] || [[ "$SOURCES_COUNT" -lt 1 ]]; then
  echo "[e2e] chat body: $CHAT_BODY" >&2
  fail "Chat response has empty sources array"
fi
pass "Chat answered (answer length=${#ANSWER}, sources=$SOURCES_COUNT)"

# ===========================================================================
# Step 10: GET /admin/stats -> total_documents>=1 and indexed>=1
# ===========================================================================
info "Fetching admin stats ..."
STATS_BODY="$(curl -s "${BASE}/admin/stats" 2>/dev/null || true)"

if [[ "$HAVE_JQ" == "1" ]]; then
  TOTAL_DOCS="$(printf '%s' "$STATS_BODY" | jq -r '.total_documents // 0' 2>/dev/null || echo 0)"
  INDEXED="$(printf '%s' "$STATS_BODY" | jq -r '.indexed // 0' 2>/dev/null || echo 0)"
else
  TOTAL_DOCS="$(printf '%s' "$STATS_BODY" | grep -oE '"total_documents"[[:space:]]*:[[:space:]]*[0-9]+' | head -n1 | grep -oE '[0-9]+' || echo 0)"
  INDEXED="$(printf '%s' "$STATS_BODY" | grep -oE '"indexed"[[:space:]]*:[[:space:]]*[0-9]+' | head -n1 | grep -oE '[0-9]+' || echo 0)"
fi

if [[ ! "$TOTAL_DOCS" =~ ^[0-9]+$ ]] || [[ "$TOTAL_DOCS" -lt 1 ]]; then
  echo "[e2e] stats body: $STATS_BODY" >&2
  fail "admin stats total_documents < 1 (got '$TOTAL_DOCS')"
fi
if [[ ! "$INDEXED" =~ ^[0-9]+$ ]] || [[ "$INDEXED" -lt 1 ]]; then
  echo "[e2e] stats body: $STATS_BODY" >&2
  fail "admin stats indexed < 1 (got '$INDEXED')"
fi
pass "Admin stats OK (total_documents=$TOTAL_DOCS, indexed=$INDEXED)"

# ===========================================================================
# Summary
# ===========================================================================
echo
echo "[e2e] =============================================="
echo "[e2e] SUMMARY: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
echo "[e2e] =============================================="

if [[ "$FAIL_COUNT" -eq 0 ]]; then
  echo "[e2e] ALL CHECKS PASSED"
  exit 0
else
  echo "[e2e] SOME CHECKS FAILED" >&2
  exit 1
fi
