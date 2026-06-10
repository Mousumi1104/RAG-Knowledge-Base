// Typed API client for the RAG Chatbot FastAPI backend.
//
// Reads the backend base URL from NEXT_PUBLIC_API_BASE_URL and exposes typed
// functions for documents, search and chat. All functions throw an Error with
// the parsed backend `detail` message on a non-OK response.

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

// ---------------------------------------------------------------------------
// Auth (single-admin JWT bearer)
// ---------------------------------------------------------------------------

const TOKEN_KEY = "admin_token";

/** Thrown when an authed request fails authentication (401). */
export class AuthError extends Error {
  constructor(message = "Authentication required") {
    super(message);
    this.name = "AuthError";
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.removeItem(TOKEN_KEY);
}

export function isAuthed(): boolean {
  return getToken() !== null;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
}

/**
 * Authenticate against the backend OAuth2 password flow.
 *
 * The backend expects `application/x-www-form-urlencoded` fields `username` and
 * `password` (FastAPI `OAuth2PasswordRequestForm`). On success the access token
 * is stored in localStorage under `admin_token` and returned.
 */
export async function login(username: string, password: string): Promise<LoginResponse> {
  const form = new URLSearchParams();
  form.append("username", username);
  form.append("password", password);
  const res = await fetch(`${API_BASE_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form.toString(),
  });
  const data = await handleJson<LoginResponse>(res);
  setToken(data.access_token);
  return data;
}

/**
 * Fetch wrapper that injects the bearer token. On a 401 it clears the stored
 * token and throws an {@link AuthError} so callers can redirect to login.
 */
export async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  if (res.status === 401) {
    clearToken();
    throw new AuthError(await extractError(res));
  }
  return res;
}

// ---------------------------------------------------------------------------
// Types (mirror backend Pydantic schemas)
// ---------------------------------------------------------------------------

export interface DocumentOut {
  id: number;
  status: number;
  added_time: string;
  last_modified: string;
  filename: string;
  document_type: string;
  uploaded_by: string | null;
  file_path: string;
  size: number;
  indexing_status: string;
  chunk_count: number;
  error: string | null;
}

export interface DocumentList {
  items: DocumentOut[];
  total: number;
}

export interface MessageResponse {
  detail: string;
}

export interface ReindexResponse {
  id: number;
  indexing_status: string;
  detail: string;
}

// Admin dashboard shapes (mirror backend /admin/* responses). Fields are kept
// permissive because the exact backend payload may evolve; the dashboard reads
// known keys defensively.

export interface AdminStats {
  total_documents: number;
  total_chunks: number;
  indexed: number;
  failed: number;
  pending: number;
  processing: number;
  vector_db_health: Record<string, unknown>;
  vector_count: number | null;
}

export interface IndexingStatusItem {
  id: number;
  filename: string;
  indexing_status: string;
  chunk_count: number;
  error?: string | null;
}

export interface IndexingStatus {
  items: IndexingStatusItem[];
  total?: number;
  [key: string]: unknown;
}

export interface AdminHealth {
  status?: string;
  vector_store?: unknown;
  database?: unknown;
  [key: string]: unknown;
}

export interface SearchResult {
  document_id: number;
  document_name: string;
  excerpt: string;
  chunk_id: string;
  score: number;
}

export interface SearchResponse {
  query: string;
  results: SearchResult[];
}

export interface ChatMessage {
  role: string;
  content: string;
}

export interface ChatSource {
  document: string;
  excerpt: string;
  document_id: number;
  page?: number | null;
  score: number;
}

/**
 * Build the public inline-file URL for a source document.
 *
 * Returns `${API_BASE_URL}/documents/${documentId}/file`, appending a
 * `#page=N` fragment only when `page` is a positive number so PDF viewers
 * open directly at the cited page.
 */
export function documentFileUrl(documentId: number, page?: number | null): string {
  const url = `${API_BASE_URL}/documents/${documentId}/file`;
  if (typeof page === "number" && page > 0) {
    return `${url}#page=${page}`;
  }
  return url;
}

export interface ChatResponse {
  answer: string;
  sources: ChatSource[];
  conversation_id?: number | null;
  message_id?: number | null;
}

// Persisted conversation/message shapes (mirror app/schemas/conversation.py).

export interface ConversationSummary {
  id: number;
  title: string | null;
  added_time: string;
  last_modified: string;
}

export interface ConversationList {
  items: ConversationSummary[];
  total: number;
}

export interface ChatMessageRecord {
  id: number;
  role: "user" | "assistant";
  content: string;
  sources: ChatSource[] | null;
  added_time: string;
}

export interface ConversationDetail extends ConversationSummary {
  messages: ChatMessageRecord[];
}

export interface StreamDonePayload {
  sources: ChatSource[];
  conversation_id: number;
  message_id: number;
}

export interface StreamChatParams {
  query: string;
  conversationId?: number;
  onDelta: (text: string) => void;
  onDone: (payload: StreamDonePayload) => void;
  onError: (message: string) => void;
  signal?: AbortSignal;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/** Extract a human-readable error message from a non-OK response. */
async function extractError(res: Response): Promise<string> {
  try {
    const data: unknown = await res.json();
    if (data && typeof data === "object" && "detail" in data) {
      const detail = (data as { detail: unknown }).detail;
      if (typeof detail === "string") return detail;
      // FastAPI validation errors return an array of objects.
      return JSON.stringify(detail);
    }
    return JSON.stringify(data);
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

async function handleJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    throw new Error(await extractError(res));
  }
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Documents
// ---------------------------------------------------------------------------

/** Extract a `detail` message from a raw response body string (XHR path). */
function extractErrorFromText(text: string, status: number, statusText: string): string {
  try {
    const data: unknown = JSON.parse(text);
    if (data && typeof data === "object" && "detail" in data) {
      const detail = (data as { detail: unknown }).detail;
      if (typeof detail === "string") return detail;
      // FastAPI validation errors return an array of objects.
      return JSON.stringify(detail);
    }
    return JSON.stringify(data);
  } catch {
    return text || `${status} ${statusText}`;
  }
}

/**
 * Upload a document via XMLHttpRequest so upload progress can be reported.
 *
 * Unlike `fetch`, XHR exposes `upload.onprogress`, letting callers drive a
 * progress bar. POSTs FormData (field name `file`, matching the backend) to
 * `/documents/upload`. An `Authorization: Bearer` header is sent only when a
 * token is supplied (admin path); the public chat path passes no token. The
 * promise resolves with the parsed {@link DocumentOut} on a 2xx response and
 * rejects with a parsed `detail` error otherwise (mirroring `extractError`).
 *
 * `onProgress` receives a fraction in `[0, 1]` while bytes are uploading, or
 * `null` when the total length is unknown (non-computable progress).
 */
export function uploadDocumentWithProgress({
  file,
  uploadedBy,
  token,
  onProgress,
}: {
  file: File;
  uploadedBy?: string;
  token?: string | null;
  onProgress?: (fraction: number | null) => void;
}): Promise<DocumentOut> {
  return new Promise<DocumentOut>((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    if (uploadedBy) {
      form.append("uploaded_by", uploadedBy);
    }

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE_URL}/documents/upload`);
    if (token) {
      xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    }

    xhr.upload.onprogress = (e: ProgressEvent) => {
      onProgress?.(e.lengthComputable ? e.loaded / e.total : null);
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as DocumentOut);
        } catch (e) {
          reject(e instanceof Error ? e : new Error(String(e)));
        }
        return;
      }
      reject(new Error(extractErrorFromText(xhr.responseText, xhr.status, xhr.statusText)));
    };

    xhr.onerror = () => {
      reject(new Error("Network error during upload."));
    };

    xhr.send(form);
  });
}

export async function uploadDocument(file: File, uploadedBy?: string): Promise<DocumentOut> {
  // Back-compat: delegate to the progress-capable uploader using the admin token.
  return uploadDocumentWithProgress({ file, uploadedBy, token: getToken() });
}

export async function listDocuments(): Promise<DocumentList> {
  const res = await authedFetch(`/documents`, {
    method: "GET",
    cache: "no-store",
  });
  return handleJson<DocumentList>(res);
}

export async function deleteDocument(id: number): Promise<MessageResponse> {
  const res = await authedFetch(`/documents/${id}`, {
    method: "DELETE",
  });
  return handleJson<MessageResponse>(res);
}

export async function reindexDocument(id: number): Promise<ReindexResponse> {
  const res = await authedFetch(`/documents/${id}/reindex`, {
    method: "POST",
  });
  return handleJson<ReindexResponse>(res);
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

export async function search(query: string, topK?: number): Promise<SearchResponse> {
  const body: { query: string; top_k?: number } = { query };
  if (typeof topK === "number") {
    body.top_k = topK;
  }
  const res = await fetch(`${API_BASE_URL}/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return handleJson<SearchResponse>(res);
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

export async function chatQuery(
  query: string,
  topK?: number,
  history?: ChatMessage[],
): Promise<ChatResponse> {
  const body: { query: string; top_k?: number; history?: ChatMessage[] } = {
    query,
  };
  if (typeof topK === "number") {
    body.top_k = topK;
  }
  if (history && history.length > 0) {
    body.history = history;
  }
  const res = await fetch(`${API_BASE_URL}/chat/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return handleJson<ChatResponse>(res);
}

// ---------------------------------------------------------------------------
// Chat streaming (SSE)
// ---------------------------------------------------------------------------

/** Route a single parsed `data:` SSE payload to the relevant handler. */
function dispatchSseEvent(
  payload: unknown,
  params: Pick<StreamChatParams, "onDelta" | "onDone" | "onError">,
): void {
  if (!payload || typeof payload !== "object") {
    return;
  }
  const data = payload as Record<string, unknown>;
  if (typeof data.delta === "string") {
    params.onDelta(data.delta);
    return;
  }
  if (typeof data.error === "string") {
    params.onError(data.error);
    return;
  }
  if (data.done === true) {
    params.onDone({
      sources: Array.isArray(data.sources) ? (data.sources as ChatSource[]) : [],
      conversation_id: typeof data.conversation_id === "number" ? data.conversation_id : 0,
      message_id: typeof data.message_id === "number" ? data.message_id : 0,
    });
  }
}

/** Parse one SSE frame (the text between blank lines) and dispatch its data. */
function handleSseFrame(
  frame: string,
  params: Pick<StreamChatParams, "onDelta" | "onDone" | "onError">,
): void {
  const dataLines: string[] = [];
  for (const rawLine of frame.split("\n")) {
    const line = rawLine.replace(/\r$/, "");
    if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }
  if (dataLines.length === 0) {
    return;
  }
  const dataStr = dataLines.join("\n");
  try {
    dispatchSseEvent(JSON.parse(dataStr) as unknown, params);
  } catch {
    // Ignore malformed frames; the stream may still recover.
  }
}

/**
 * Stream a grounded answer over SSE.
 *
 * POSTs to `/chat/stream`, reads the response body with a reader + TextDecoder,
 * buffers the bytes, splits into SSE frames on blank lines and routes each
 * `data:` payload to `onDelta` / `onDone` / `onError`. A non-OK initial
 * response is surfaced through `onError`.
 */
export async function streamChat(params: StreamChatParams): Promise<void> {
  const { query, conversationId, onDelta, onDone, onError, signal } = params;
  const body: { query: string; conversation_id?: number } = { query };
  if (typeof conversationId === "number") {
    body.conversation_id = conversationId;
  }

  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/chat/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(body),
      signal,
    });
  } catch (e) {
    onError(e instanceof Error ? e.message : String(e));
    return;
  }

  if (!res.ok) {
    onError(await extractError(res));
    return;
  }
  if (!res.body) {
    onError("Streaming is not supported by this browser.");
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let sepIndex = buffer.indexOf("\n\n");
      while (sepIndex !== -1) {
        const frame = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        handleSseFrame(frame, { onDelta, onDone, onError });
        sepIndex = buffer.indexOf("\n\n");
      }
    }
    // Flush any trailing frame not terminated by a blank line.
    buffer += decoder.decode();
    if (buffer.trim().length > 0) {
      handleSseFrame(buffer, { onDelta, onDone, onError });
    }
  } catch (e) {
    if (signal?.aborted) {
      return;
    }
    onError(e instanceof Error ? e.message : String(e));
  }
}

// ---------------------------------------------------------------------------
// Conversations
// ---------------------------------------------------------------------------

export async function getConversations(): Promise<ConversationSummary[]> {
  const res = await fetch(`${API_BASE_URL}/chat/conversations`, {
    method: "GET",
    cache: "no-store",
  });
  const data = await handleJson<ConversationList>(res);
  return data.items;
}

export async function getConversation(id: number): Promise<ConversationDetail> {
  const res = await fetch(`${API_BASE_URL}/chat/conversations/${id}`, {
    method: "GET",
    cache: "no-store",
  });
  return handleJson<ConversationDetail>(res);
}

export async function deleteConversation(id: number): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/chat/conversations/${id}`, {
    method: "DELETE",
  });
  await handleJson<MessageResponse>(res);
}

// ---------------------------------------------------------------------------
// Admin (all protected via authedFetch)
// ---------------------------------------------------------------------------

export async function getMe(): Promise<{ username: string }> {
  const res = await authedFetch(`/auth/me`, { method: "GET", cache: "no-store" });
  return handleJson<{ username: string }>(res);
}

export async function getStats(): Promise<AdminStats> {
  const res = await authedFetch(`/admin/stats`, { method: "GET", cache: "no-store" });
  return handleJson<AdminStats>(res);
}

export async function getIndexingStatus(): Promise<IndexingStatus> {
  const res = await authedFetch(`/admin/indexing-status`, { method: "GET", cache: "no-store" });
  return handleJson<IndexingStatus>(res);
}

export async function getAdminHealth(): Promise<AdminHealth> {
  const res = await authedFetch(`/admin/health`, { method: "GET", cache: "no-store" });
  return handleJson<AdminHealth>(res);
}

export async function triggerReindex(documentId?: number): Promise<MessageResponse> {
  const body =
    typeof documentId === "number" ? JSON.stringify({ document_id: documentId }) : undefined;
  const res = await authedFetch(`/admin/reindex`, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body,
  });
  return handleJson<MessageResponse>(res);
}
