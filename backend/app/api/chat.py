"""RAG chat API routes.

Thin router: validate input (FastAPI/pydantic yields 422 on an empty query),
delegate the RAG pipeline to ``chat_service``, and shape the JSON response.

Endpoints:
- ``POST /chat/query``  — non-streaming answer (back-compat) + persistence.
- ``POST /chat/stream`` — Server-Sent-Events token stream + persistence.
- ``GET  /chat/conversations``       — list ACTIVE conversations (newest first).
- ``GET  /chat/conversations/{id}``  — one conversation with ordered messages.
- ``DELETE /chat/conversations/{id}`` — soft-delete a conversation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.chat import ChatRequest, ChatResponse, Source
from app.schemas.conversation import (
    ConversationDetail,
    ConversationList,
    ConversationOut,
    MessageOut,
)
from app.schemas.document import MessageResponse
from app.services import chat_service
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["chat"])

# Headers that keep SSE frames flowing through dev servers and proxies.
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@router.post("/chat/query", response_model=ChatResponse)
def chat_query(
    payload: ChatRequest,
    db: Session = Depends(get_db),
) -> ChatResponse:
    """Answer a natural-language question with RAG over indexed documents.

    Retrieves the most relevant ACTIVE chunks, builds a strictly-grounded
    prompt, generates an answer with the configured LLM provider (falling back
    to a deterministic local extractive provider on provider errors), persists
    the turn, and returns the answer plus traceable source references and the
    resolved ``conversation_id``/``message_id``.
    """
    history = [message.model_dump() for message in payload.history] if payload.history else None
    logger.info(
        "POST /chat/query query=%r top_k=%s conversation_id=%s",
        payload.query,
        payload.top_k,
        payload.conversation_id,
    )

    result = chat_service.query(
        db,
        question=payload.query,
        top_k=payload.top_k,
        history=history,
        conversation_id=payload.conversation_id,
    )

    sources = [Source(**source) for source in result.get("sources", [])]
    return ChatResponse(
        answer=result.get("answer", ""),
        sources=sources,
        conversation_id=result.get("conversation_id"),
        message_id=result.get("message_id"),
    )


@router.post("/chat/stream")
def chat_stream(payload: ChatRequest) -> StreamingResponse:
    """Stream a grounded answer as Server-Sent Events while persisting the turn.

    Emits ``data: {"delta": ...}`` frames per token, a terminal
    ``data: {"done": true, "sources", "conversation_id", "message_id"}`` frame,
    or ``data: {"error": ...}`` on failure. The streaming generator owns its own
    DB session (a ``Depends(get_db)`` session would already be closed by the time
    the body streams).
    """
    history = [message.model_dump() for message in payload.history] if payload.history else None
    logger.info(
        "POST /chat/stream query=%r top_k=%s conversation_id=%s",
        payload.query,
        payload.top_k,
        payload.conversation_id,
    )

    def event_generator() -> Iterator[str]:
        for kind, value in chat_service.stream_answer(
            question=payload.query,
            top_k=payload.top_k,
            history=history,
            conversation_id=payload.conversation_id,
        ):
            if kind == "delta":
                yield f"data: {json.dumps({'delta': value})}\n\n"
            elif kind == "done":
                yield f"data: {json.dumps({'done': True, **value})}\n\n"
            elif kind == "error":
                yield f"data: {json.dumps({'error': value})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/chat/conversations", response_model=ConversationList)
def list_conversations(db: Session = Depends(get_db)) -> ConversationList:
    """List ACTIVE conversations, newest first."""
    conversations = chat_service.list_conversations(db)
    items = [ConversationOut.model_validate(c) for c in conversations]
    return ConversationList(items=items, total=len(items))


@router.get("/chat/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
) -> ConversationDetail:
    """Return one ACTIVE conversation with its chronological ACTIVE messages.

    Raises 404 (via ``NotFoundError``) when the conversation is missing/deleted.
    """
    conversation = chat_service.get_conversation_or_404(db, conversation_id)
    messages = chat_service.get_conversation_messages(db, conversation_id)
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        added_time=conversation.added_time,
        last_modified=conversation.last_modified,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.delete("/chat/conversations/{conversation_id}", response_model=MessageResponse)
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Soft-delete a conversation (status = DELETED) and its messages."""
    chat_service.soft_delete_conversation(db, conversation_id)
    return MessageResponse(detail=f"Conversation {conversation_id} deleted")
