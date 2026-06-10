# RAG Chatbot - Frontend (Next.js POC)

A minimal Next.js (App Router, TypeScript) frontend for the RAG Chatbot FastAPI
backend. It lets you upload documents, run semantic search, and chat with
grounded answers that cite their sources.

## Prerequisites

- Node.js 18.18+ (or 20+)
- The FastAPI backend running and reachable (default `http://127.0.0.1:8000`).

## Setup

```bash
cd frontend
npm install
```

Optionally point the app at a non-default backend by creating `.env.local`
(see `.env.local.example`):

```bash
cp .env.local.example .env.local
# edit NEXT_PUBLIC_API_BASE_URL if your backend is not on http://127.0.0.1:8000
```

## Run (development)

```bash
npm run dev
```

Open http://localhost:3000. Make sure the backend is running on port 8000
(or whatever you configured via `NEXT_PUBLIC_API_BASE_URL`).

## Build / Production

```bash
npm run build
npm run start
```

## Features

- **Documents** (`components/DocumentUpload.tsx`): upload a file, view the
  document list with indexing status / chunk count, reindex, and delete.
- **Chat** (`components/ChatInterface.tsx`): ask questions; the answer is shown
  with a **Sources** section (document name + excerpt per source). Conversation
  history is kept in state and sent with each follow-up.
- **Semantic Search** (`components/SearchResults.tsx`): list ranked hits with
  document name, score, and excerpt.

## Notes

- The typed API client lives in `services/api.ts` and reads
  `NEXT_PUBLIC_API_BASE_URL` (defaults to `http://127.0.0.1:8000`).
- CORS: the backend must allow requests from `http://localhost:3000`.
