# AI-Powered RAG Knowledge Base / Chatbot Requirements

This project contains:

* Backend API using FastAPI
* PostgreSQL for metadata storage
* Vector Database (ChromaDB / PGVector)
* OpenAI or Claude API integration
* Optional React / Next.js frontend for Proof of Concept

The goal is to build an AI-powered Knowledge Base and Chatbot system that enables users to query internal company documents using natural language through Retrieval-Augmented Generation (RAG).

---

# 🔒 Architecture & API Rules (Mandatory)

Claude must maintain:

* Clear separation between:

  * API layer
  * Document processing layer
  * Embedding/indexing layer
  * LLM response generation layer

* Modular service structure

* Config-driven architecture

* Environment variable support for secrets/API keys

The system must support future scaling and additional document types without major refactoring.

---

# 📌 Common Database Structure Rules

All database tables must include the following common fields:

```sql
id              BIGINT PRIMARY KEY AUTO_INCREMENT
status          SMALLINT NOT NULL DEFAULT 1
added_time      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
last_modified   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
```

## Status Values

| Value | Meaning             |
| ----- | ------------------- |
| 0     | Inactive   |
| 1     | Active              |
| 5     | Deleted |

Requirements:

* Every table must include these fields
* Prefer soft delete using:

```sql
status = 0
```

* Reusable ORM base model should be used

---

# 1. Document Management

## Supported File Types

The system must support:

* PDF
* DOCX
* TXT
* Markdown (.md)

---

## Upload Requirements

Users/admins should be able to:

* Upload documents
* View uploaded documents
* Delete documents
* Re-upload updated versions

---

## Metadata Requirements

Store metadata in PostgreSQL:

```json
{
  "id": 1,
  "filename": "company_policy.pdf",
  "document_type": "pdf",
  "uploaded_by": "admin",
  "uploaded_at": "2026-06-01T10:00:00",
  "status": 1
}
```

---

## Document Processing

Implement:

* Text extraction
* Text cleaning
* Chunking strategy
* Chunk overlap support
* Token-safe chunk sizing

Chunking should preserve semantic meaning where possible.

---

# 2. Knowledge Indexing

## Embedding Generation

Generate embeddings using:

* OpenAI Embeddings API
* Claude-compatible embeddings
* Or local embedding models

Store vectors in:

* ChromaDB
* PGVector
* Or equivalent vector database

---

## Indexing Requirements

Each chunk must store:

```json
{
  "document_id": 1,
  "chunk_id": "abc123",
  "content": "...",
  "embedding": [...]
}
```

Support:

* Incremental indexing
* Re-indexing after updates
* Vector deletion on document removal

---

## Indexing Status

Track indexing state:

* Pending
* Processing
* Indexed
* Failed

Admin should be able to view indexing statistics.

---

# 3. Semantic Search

## Expected Behavior

Users should be able to search using natural language.

Example:

> “What is the leave approval process?”

The system should:

* Convert query into embedding
* Perform vector similarity search
* Return top matching chunks

---

## Search Response

```json
{
  "document_name": "HR_Policy.pdf",
  "excerpt": "Employees must apply for leave...",
  "score": 0.92
}
```

Requirements:

* Semantic similarity ranking
* Configurable top-k retrieval
* Most relevant chunks first

---

# 4. Chat / RAG Functionality

## System Flow

1. Receive user query
2. Retrieve relevant chunks
3. Build contextual prompt
4. Send prompt to LLM
5. Generate grounded answer
6. Return sources

---

## Response Format

```json
{
  "answer": "Employees must apply for leave using...",
  "sources": [
    {
      "document": "HR_Policy.pdf",
      "excerpt": "Employees must apply for leave..."
    }
  ]
}
```

Requirements:

* Responses must use retrieved context
* Avoid hallucination
* Include source references
* Support follow-up questions

---

# 5. Source References

Every response should display:

* Document names
* Relevant excerpts
* Traceable references

Users must identify exactly which documents contributed to the answer.

---

# 6. Admin Features

Admin users should be able to:

* Upload documents
* Delete documents
* Trigger re-indexing
* Monitor indexing status
* View document statistics

Statistics include:

* Total documents
* Total chunks
* Indexed vs failed documents
* Vector database health

---

# 7. API Requirements

## Suggested APIs

### Document APIs

```http
POST /documents/upload
GET /documents
DELETE /documents/{id}
POST /documents/{id}/reindex
```

### Search API

```http
POST /search
```

### Chat API

```http
POST /chat/query
```

Requirements:

* Structured JSON responses
* Proper validation handling
* Meaningful error responses

---

# 8. Frontend (Optional POC)

If frontend is implemented:

Use:

* React
* Next.js

Features:

* Document upload UI
* Chat interface
* Search results
* Source reference section

UI should remain minimal and functional.

---

# 9. Suggested Project Structure

```bash
backend/
├── app/
│   ├── api/
│   ├── services/
│   ├── rag/
│   ├── vectorstore/
│   ├── database/
│   ├── models/
│   ├── schemas/
│   └── utils/
│
├── uploads/
├── requirements.txt
└── main.py

frontend/
├── src/
├── components/
├── pages/
└── services/
```

---

# 10. Implementation Order

## Step 1: Backend Foundation

* Setup FastAPI
* Configure PostgreSQL
* Configure vector database

## Step 2: Document Processing

* Upload APIs
* Parsers
* Chunking

## Step 3: Embedding & Indexing

* Generate embeddings
* Store vectors
* Track indexing

## Step 4: Semantic Search

* Similarity search
* Ranked chunk retrieval

## Step 5: RAG Chat

* LLM integration
* Contextual prompting
* Grounded answers

## Step 6: Admin & UI

* Admin features
* Optional frontend

## Step 7: Final Validation

* Upload works
* Indexing works
* Search works
* RAG works
* Sources displayed correctly

---

# Deliverables

* Working AI-powered RAG Knowledge Base
* Semantic search
* RAG chatbot
* Source references
* Modular scalable architecture
