# Employee Zone Chat API — Frontend Hand-off

Short guide for the mobile team. These are the **new `/employee/*` endpoints** for the in-app chatbot.

- **Base URL (live Render):** `https://rag-knowledge-base-86tb.onrender.com` *(confirm the exact host in the Render dashboard)*
- **Auth right now:** **none required** — authentication is intentionally OFF for this first version. Just call the endpoints.
- **Optional identity:** to keep each employee's chat history separate, send these headers (unverified for now):
  - `X-Employee-Email: jane@company.com` *(recommended — used to separate conversations per person)*
  - `X-Employee-Id: 12345` *(optional)*
  - `X-Employee-Name: Jane Doe` *(optional)*
  - If you send nothing, everyone shares one default identity.
- Employees can **only ask questions**. Answers come **only from the company documents** (admin-uploaded). There is **no upload** endpoint here.
- Later, real Employee Zone token login will be added (you'll send the EZ `access_token` as a `Bearer` header) — no endpoint paths will change.

---

## Endpoints

### 1. Ask a question (non-streaming) — main one
`POST /employee/chat/query`

Request:
```json
{ "query": "How many casual leaves do I get?", "top_k": 8, "conversation_id": null }
```
- `query` (required), `top_k` (optional), `conversation_id` (optional — omit to start a new chat), `history` (optional).

Response:
```json
{
  "answer": "You are entitled to ...",
  "sources": [{ "document": "HR_Policy.pdf", "excerpt": "...", "document_id": 12, "page": 4 }],
  "conversation_id": 101,
  "message_id": 555
}
```

curl:
```bash
curl -X POST https://rag-knowledge-base-86tb.onrender.com/employee/chat/query \
  -H "Content-Type: application/json" \
  -H "X-Employee-Email: jane@company.com" \
  -d '{"query":"How many casual leaves do I get?"}'
```

### 2. Ask a question (streaming, live typing) — optional
`POST /employee/chat/stream` — Server-Sent Events.
Frames: `data: {"delta":"..."}` per token, then `data: {"done":true,"sources":[...],"conversation_id":N,"message_id":M}`.
Note: this is a **POST** (not browser `EventSource`). Use `/employee/chat/query` if streaming is fiddly on mobile.

**Errors / quota exhaustion:** you always get a rendered answer, never a blank screen. If a provider (embedding or LLM) fails or its quota is exhausted, `/query` still returns `200` with a friendly `answer` (empty `sources`), and `/stream` emits that message as a normal `delta`+`done` pair. (A legacy `data: {"error":"..."}` frame is no longer emitted for provider failures.)

### 3. Start a new conversation
`POST /employee/chat/conversations` → `201` `{ "id": 101, ... }`
(Optional — you can also just omit `conversation_id` on the first `query`.)

### 4. List my conversations
`GET /employee/chat/conversations` → `{ "items": [ ... ], "total": N }` (newest first)

### 5. Get one conversation (with messages)
`GET /employee/chat/conversations/{conversation_id}` → conversation + ordered messages. `404` if it isn't yours.

### 6. Delete a conversation
`DELETE /employee/chat/conversations/{conversation_id}` → `{ "detail": "Conversation N deleted" }`

### 7. Who am I (identity echo)
`GET /employee/auth/me` →
```json
{ "id": 7, "email": "jane@company.com", "role": "user", "emp_id": "12345", "full_name": "Jane Doe" }
```

---

## What the backend still needs from you (for the next phase: real login)
1. How should we **verify** the Employee Zone `access_token`? One of: (a) a URL we POST the token to that confirms it, (b) your public key / JWKS URL, or (c) a shared secret.
2. Confirm the exact **issuer / audience** values in your tokens (if JWT).
3. Confirm the live **Render host**.

Until then, the app integrates with **no auth** using the optional `X-Employee-*` headers above.
