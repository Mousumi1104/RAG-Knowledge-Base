#!/usr/bin/env bash
#
# chat_e2e.sh — Live end-to-end test of the RAG chat feature (streaming + persistence).
#
# Boots the FastAPI app against the live local PostgreSQL (pgvector) with local
# providers and exercises the chat flow end-to-end with curl:
#   upload -> index -> SSE stream -> conversation persistence -> continue turn ->
#   non-streaming back-compat /chat/query.
#
# Each step asserts real results and prints [chat-e2e] PASS/FAIL; a final
# SUMMARY line reports counts. Exit 0 only when every check passed.
#
# Contract sources (read from routers + pydantic schemas):
#   - upload multipart field        = "file"  (POST /documents/upload -> 201, DocumentOut)
#   - indexed status field/value    = indexing_status == "indexed"  (DocumentOut)
#   - POST /chat/stream body        = {"query":..., "conversation_id":optional}  (ChatRequest)
#   - SSE delta frame               = data: {"delta": "..."}
#   - SSE done frame                = data: {"done": true, "sources":[...],
#                                            "conversation_id": N, "message_id": N}
#   - GET /chat/conversations       = {"items":[{id,title,added_time,last_modified}], "total"}  (ConversationList)
#   - GET /chat/conversations/{id}  = {id,title,added_time,last_modified,
#                                      "messages":[{id,role,content,sources,added_time}]} (ConversationDetail)
#   - POST /chat/query              = {"answer", "sources":[{document,excerpt,document_id,score}],
#                                      "conversation_id", "message_id"}  (ChatResponse)
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

UVICORN_LOG="$(mktemp -t chat_e2e_uvicorn.XXXXXX.log)"
TMPFILE="$(mktemp -t chat_e2e_doc.XXXXXX.txt)"
STREAM1="$(mktemp -t chat_e2e_stream1.XXXXXX.sse)"
STREAM2="$(mktemp -t chat_e2e_stream2.XXXXXX.sse)"
PID=""

PASS_COUNT=0
FAIL_COUNT=0

# --- Logging helpers --------------------------------------------------------
pass() { echo "[chat-e2e] PASS: $*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() {
  echo "[chat-e2e] FAIL: $*" >&2
  FAIL_COUNT=$((FAIL_COUNT + 1))
  echo "[chat-e2e] ----- uvicorn log (tail) -----" >&2
  tail -n 60 "$UVICORN_LOG" >&2 2>/dev/null || true
  echo "[chat-e2e] ------------------------------" >&2
  exit 1
}
info() { echo "[chat-e2e] $*"; }

# --- Cleanup ----------------------------------------------------------------
cleanup() {
  if [[ -n "${PID:-}" ]]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -f "$TMPFILE" "$UVICORN_LOG" "$STREAM1" "$STREAM2" 2>/dev/null || true
}
trap cleanup EXIT

# --- jq detection -----------------------------------------------------------
HAVE_JQ=0
if command -v jq >/dev/null 2>&1; then
  HAVE_JQ=1
fi

# ===========================================================================
# Step 1: Ensure PostgreSQL is up + apply Alembic migrations
# ===========================================================================
info "Ensuring local PostgreSQL is up ..."
if bash "$REPO_ROOT/backend/scripts/pg_local.sh" up; then
  pass "PostgreSQL is up"
else
  fail "PostgreSQL failed to start"
fi

info "Applying Alembic migrations (upgrade head) ..."
if (cd "$BACKEND_DIR" && DATABASE_URL="$DATABASE_URL" ./venv/bin/alembic upgrade head); then
  pass "Alembic upgrade head"
else
  fail "Alembic upgrade head failed"
fi

# ===========================================================================
# Step 2: Start uvicorn in the background + poll /health until 200 (~40s)
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
# Step 3: Upload a known .txt and wait until indexed
# ===========================================================================
cat >"$TMPFILE" <<'EOF'
Employees must submit leave requests through the HR portal for approval. Managers approve within two business days.
EOF
info "Created temp document: $TMPFILE"

info "Uploading document ..."
UPLOAD_BODY="$(mktemp -t chat_e2e_upload.XXXXXX.json)"
upload_code="$(curl -s -o "$UPLOAD_BODY" -w '%{http_code}' \
  -F "file=@${TMPFILE};type=text/plain" \
  "${BASE}/documents/upload" 2>/dev/null || echo 000)"

if [[ "$upload_code" != "200" && "$upload_code" != "201" ]]; then
  echo "[chat-e2e] upload response body:" >&2
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

info "Waiting for document $DOC_ID to be indexed (via GET /documents) ..."
INDEXED_OK=0
STATUS="unknown"
for _ in $(seq 1 60); do
  DOCS_BODY="$(curl -s "${BASE}/documents" 2>/dev/null || true)"
  if [[ "$HAVE_JQ" == "1" ]]; then
    STATUS="$(printf '%s' "$DOCS_BODY" | jq -r --argjson id "$DOC_ID" \
      '.items[] | select(.id == $id) | .indexing_status' 2>/dev/null | head -n1 || true)"
  else
    # Best-effort without jq: confirm the doc id appears and any "indexed" status exists.
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
  if [[ "$STATUS" == "indexed" ]]; then
    INDEXED_OK=1
    break
  fi
  if [[ "$STATUS" == "failed" ]]; then
    echo "[chat-e2e] documents body: $DOCS_BODY" >&2
    fail "Document indexing failed (indexing_status=failed)"
  fi
  sleep 0.5
done

if [[ "$INDEXED_OK" == "1" ]]; then
  pass "Document indexed (GET /documents shows indexing_status=indexed)"
else
  fail "Document did not reach indexing_status=indexed within timeout (last status=${STATUS:-unknown})"
fi

# ===========================================================================
# Step 4: STREAMING — POST /chat/stream and assert delta frames + done frame
# ===========================================================================
info "Streaming chat answer (POST /chat/stream) ..."
if ! curl -N -sS -X POST "${BASE}/chat/stream" \
  -H 'Content-Type: application/json' \
  -d '{"query":"How do employees request leave?"}' >"$STREAM1" 2>/dev/null; then
  echo "[chat-e2e] stream output:" >&2
  cat "$STREAM1" >&2 2>/dev/null || true
  fail "curl to /chat/stream failed"
fi

# Count SSE data: frames carrying a "delta" key.
DELTA_COUNT="$(grep -E '^data:' "$STREAM1" | grep -c '"delta"' || true)"
DELTA_COUNT="${DELTA_COUNT:-0}"
# Locate the terminal done frame line.
DONE_LINE="$(grep -E '^data:' "$STREAM1" | grep '"done"' | head -n1 || true)"

if [[ ! "$DELTA_COUNT" =~ ^[0-9]+$ ]] || [[ "$DELTA_COUNT" -lt 1 ]]; then
  echo "[chat-e2e] stream output:" >&2
  cat "$STREAM1" >&2 2>/dev/null || true
  fail "Stream produced no delta frames (count=$DELTA_COUNT)"
fi
if [[ -z "$DONE_LINE" ]]; then
  echo "[chat-e2e] stream output:" >&2
  cat "$STREAM1" >&2 2>/dev/null || true
  fail "Stream had no terminal done frame"
fi
if ! printf '%s' "$DONE_LINE" | grep -q '"sources"'; then
  echo "[chat-e2e] done line: $DONE_LINE" >&2
  fail "Done frame missing sources"
fi
if ! printf '%s' "$DONE_LINE" | grep -q '"conversation_id"'; then
  echo "[chat-e2e] done line: $DONE_LINE" >&2
  fail "Done frame missing conversation_id"
fi

# Extract the conversation_id from the done frame's JSON (strip leading "data: ").
DONE_JSON="${DONE_LINE#data: }"
DONE_JSON="${DONE_JSON#data:}"
if [[ "$HAVE_JQ" == "1" ]]; then
  CONV_ID="$(printf '%s' "$DONE_JSON" | jq -r '.conversation_id // empty' 2>/dev/null || true)"
else
  CONV_ID="$(printf '%s' "$DONE_JSON" | grep -oE '"conversation_id"[[:space:]]*:[[:space:]]*[0-9]+' | head -n1 | grep -oE '[0-9]+' || true)"
fi

if [[ -z "${CONV_ID:-}" ]] || [[ ! "$CONV_ID" =~ ^[0-9]+$ ]]; then
  echo "[chat-e2e] done line: $DONE_LINE" >&2
  fail "Could not extract conversation_id from done frame"
fi
pass "Streaming OK ($DELTA_COUNT delta frame(s) + done frame, conversation_id=$CONV_ID)"

# ===========================================================================
# Step 5: PERSISTENCE — list conversations + fetch detail
# ===========================================================================
info "Listing conversations (GET /chat/conversations) ..."
CONV_LIST="$(curl -s "${BASE}/chat/conversations" 2>/dev/null || true)"
if [[ "$HAVE_JQ" == "1" ]]; then
  CONV_TOTAL="$(printf '%s' "$CONV_LIST" | jq -r '.items | length' 2>/dev/null || echo 0)"
else
  CONV_TOTAL="$(printf '%s' "$CONV_LIST" | grep -oc '"id"' || echo 0)"
fi
if [[ ! "$CONV_TOTAL" =~ ^[0-9]+$ ]] || [[ "$CONV_TOTAL" -lt 1 ]]; then
  echo "[chat-e2e] conversations body: $CONV_LIST" >&2
  fail "Conversations list has no items (count=$CONV_TOTAL)"
fi
pass "Conversations list has $CONV_TOTAL item(s)"

info "Fetching conversation detail (GET /chat/conversations/$CONV_ID) ..."
CONV_DETAIL="$(curl -s "${BASE}/chat/conversations/${CONV_ID}" 2>/dev/null || true)"
if [[ "$HAVE_JQ" == "1" ]]; then
  MSG_COUNT="$(printf '%s' "$CONV_DETAIL" | jq -r '.messages | length' 2>/dev/null || echo 0)"
  ASSISTANT_CONTENT="$(printf '%s' "$CONV_DETAIL" | jq -r \
    '[.messages[] | select(.role == "assistant")] | last | .content // empty' 2>/dev/null || true)"
  ASSISTANT_SRC_COUNT="$(printf '%s' "$CONV_DETAIL" | jq -r \
    '[.messages[] | select(.role == "assistant")] | last | (.sources // []) | length' 2>/dev/null || echo 0)"
else
  MSG_COUNT="$(printf '%s' "$CONV_DETAIL" | grep -oc '"role"' || echo 0)"
  # Best-effort: any non-empty assistant content + any source object present.
  ASSISTANT_CONTENT="$(printf '%s' "$CONV_DETAIL" | grep -oE '"content"[[:space:]]*:[[:space:]]*"[^"]+"' | tail -n1 | sed -E 's/.*:[[:space:]]*"(.*)"/\1/' || true)"
  ASSISTANT_SRC_COUNT="$(printf '%s' "$CONV_DETAIL" | grep -oc '"document_id"' || echo 0)"
fi

if [[ ! "$MSG_COUNT" =~ ^[0-9]+$ ]] || [[ "$MSG_COUNT" -lt 2 ]]; then
  echo "[chat-e2e] conversation detail: $CONV_DETAIL" >&2
  fail "Conversation has < 2 messages (count=$MSG_COUNT)"
fi
if [[ -z "$ASSISTANT_CONTENT" ]]; then
  echo "[chat-e2e] conversation detail: $CONV_DETAIL" >&2
  fail "Assistant message content is empty"
fi
if [[ ! "$ASSISTANT_SRC_COUNT" =~ ^[0-9]+$ ]] || [[ "$ASSISTANT_SRC_COUNT" -lt 1 ]]; then
  echo "[chat-e2e] conversation detail: $CONV_DETAIL" >&2
  fail "Assistant message has empty sources"
fi
pass "Persistence OK ($MSG_COUNT message(s); assistant content len=${#ASSISTANT_CONTENT}, sources=$ASSISTANT_SRC_COUNT)"

# ===========================================================================
# Step 6: CONTINUE — stream a follow-up into the same conversation
# ===========================================================================
info "Continuing conversation $CONV_ID (POST /chat/stream with conversation_id) ..."
if ! curl -N -sS -X POST "${BASE}/chat/stream" \
  -H 'Content-Type: application/json' \
  -d "{\"query\":\"And who approves it?\",\"conversation_id\":${CONV_ID}}" >"$STREAM2" 2>/dev/null; then
  echo "[chat-e2e] stream2 output:" >&2
  cat "$STREAM2" >&2 2>/dev/null || true
  fail "curl to /chat/stream (continue) failed"
fi

DELTA2_COUNT="$(grep -E '^data:' "$STREAM2" | grep -c '"delta"' || true)"
DELTA2_COUNT="${DELTA2_COUNT:-0}"
DONE2_LINE="$(grep -E '^data:' "$STREAM2" | grep '"done"' | head -n1 || true)"
if [[ ! "$DELTA2_COUNT" =~ ^[0-9]+$ ]] || [[ "$DELTA2_COUNT" -lt 1 ]] || [[ -z "$DONE2_LINE" ]]; then
  echo "[chat-e2e] stream2 output:" >&2
  cat "$STREAM2" >&2 2>/dev/null || true
  fail "Continue stream produced no delta frames or no done frame"
fi

CONV_DETAIL2="$(curl -s "${BASE}/chat/conversations/${CONV_ID}" 2>/dev/null || true)"
if [[ "$HAVE_JQ" == "1" ]]; then
  MSG_COUNT2="$(printf '%s' "$CONV_DETAIL2" | jq -r '.messages | length' 2>/dev/null || echo 0)"
else
  MSG_COUNT2="$(printf '%s' "$CONV_DETAIL2" | grep -oc '"role"' || echo 0)"
fi
if [[ ! "$MSG_COUNT2" =~ ^[0-9]+$ ]] || [[ "$MSG_COUNT2" -lt 4 ]]; then
  echo "[chat-e2e] conversation detail (after continue): $CONV_DETAIL2" >&2
  fail "Conversation has < 4 messages after continue (count=$MSG_COUNT2)"
fi
pass "Continue OK (conversation now has $MSG_COUNT2 message(s))"

# ===========================================================================
# Step 7: BACK-COMPAT — non-streaming POST /chat/query
# ===========================================================================
info "Running non-streaming chat query (POST /chat/query) ..."
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
  echo "[chat-e2e] chat body: $CHAT_BODY" >&2
  fail "Back-compat /chat/query has empty answer"
fi
if [[ ! "$SOURCES_COUNT" =~ ^[0-9]+$ ]] || [[ "$SOURCES_COUNT" -lt 1 ]]; then
  echo "[chat-e2e] chat body: $CHAT_BODY" >&2
  fail "Back-compat /chat/query has empty sources array"
fi
pass "Back-compat /chat/query OK (answer len=${#ANSWER}, sources=$SOURCES_COUNT)"

# ===========================================================================
# Summary
# ===========================================================================
echo
echo "[chat-e2e] =============================================="
echo "[chat-e2e] SUMMARY: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
echo "[chat-e2e] =============================================="

if [[ "$FAIL_COUNT" -eq 0 ]]; then
  echo "[chat-e2e] ALL CHECKS PASSED"
  exit 0
else
  echo "[chat-e2e] SOME CHECKS FAILED" >&2
  exit 1
fi
