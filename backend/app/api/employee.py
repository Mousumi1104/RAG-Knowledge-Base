"""Employee Zone chat API routes (no authentication, current mode).

Mirrors the user-facing :mod:`app.api.chat` router but for Employee Zone
employees, who read ONLY the admin-curated **global** knowledge base — never any
private/personal documents. The differences from ``/chat``:

- Identity is resolved by :func:`app.core.employee_zone.resolve_employee` instead
  of a bearer token; in the current mode this requires NO token (optional,
  unverified ``X-Employee-*`` headers select/JIT-provision the backing user).
- Every retrieval uses the server-derived global-only scope
  (:data:`app.core.access.GLOBAL_ONLY` -> ``owner_id=None, include_global=True``)
  and surfaces global company-doc citations (``show_global=True``).
- Conversations are owned by the resolved employee's backing user id, so per
  employee separation works when the app sends ``X-Employee-Email``.
- There is intentionally NO employee upload/document endpoint.

Responses and SSE frames are byte-compatible with the ``/chat`` API.

Endpoints:
- ``POST /employee/chat/query``  — non-streaming grounded answer + persistence.
- ``POST /employee/chat/stream`` — SSE token stream + persistence.
- ``POST /employee/chat/conversations``      — start a fresh empty conversation.
- ``GET  /employee/chat/conversations``      — list the employee's conversations.
- ``GET  /employee/chat/conversations/{id}`` — one conversation with messages.
- ``DELETE /employee/chat/conversations/{id}`` — soft-delete a conversation.
- ``GET  /employee/auth/me`` — the resolved employee identity.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core import access
from app.core.employee_zone import (
    _HEADER_EMPLOYEE_ID,
    _HEADER_EMPLOYEE_NAME,
    resolve_employee,
)
from app.database.session import get_db
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse, Source
from app.schemas.conversation import (
    ConversationDetail,
    ConversationList,
    ConversationOut,
    MessageOut,
)
from app.schemas.document import MessageResponse
from app.schemas.employee import EmployeeMeResponse
from app.services import chat_service
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/employee", tags=["employee"])

# Headers that keep SSE frames flowing through dev servers and proxies (identical
# to the /chat router so frames stay byte-compatible).
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def get_current_employee(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency resolving the current Employee Zone employee to a User.

    Delegates to :func:`app.core.employee_zone.resolve_employee`, which in the
    current (auth-disabled) mode requires NO token and resolves identity from the
    optional ``X-Employee-*`` headers (falling back to the shared default email).
    """
    return resolve_employee(db, request)


@router.post("/chat/query", response_model=ChatResponse)
def employee_chat_query(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> ChatResponse:
    """Answer a question with RAG over the GLOBAL company knowledge base only.

    Mirrors :func:`app.api.chat.chat_query` but pins retrieval to the global-only
    scope and surfaces global company-doc citations. The turn is persisted to a
    conversation owned by the resolved employee.
    """
    history = [message.model_dump() for message in payload.history] if payload.history else None
    owner_id, include_global = access.GLOBAL_ONLY
    logger.info(
        "POST /employee/chat/query query=%r top_k=%s conversation_id=%s employee_id=%s",
        payload.query,
        payload.top_k,
        payload.conversation_id,
        employee.id,
    )

    result = chat_service.query(
        db,
        question=payload.query,
        top_k=payload.top_k,
        history=history,
        conversation_id=payload.conversation_id,
        owner_id=employee.id,
        retrieval_owner_id=owner_id,
        include_global=include_global,
        show_global=True,
    )

    sources = [Source(**source) for source in result.get("sources", [])]
    return ChatResponse(
        answer=result.get("answer", ""),
        sources=sources,
        conversation_id=result.get("conversation_id"),
        message_id=result.get("message_id"),
    )


@router.post("/chat/stream")
def employee_chat_stream(
    payload: ChatRequest,
    employee: User = Depends(get_current_employee),
) -> StreamingResponse:
    """Stream a grounded answer (global-only scope) as Server-Sent Events.

    Mirrors :func:`app.api.chat.chat_stream` with identical SSE frames; retrieval
    is pinned to the global-only scope and the turn is persisted to a conversation
    owned by the resolved employee.
    """
    history = [message.model_dump() for message in payload.history] if payload.history else None
    # Capture ids as plain ints: the SSE generator runs AFTER the request-scoped
    # DB session closes, so the ORM ``User`` must not cross into it.
    owner_id = employee.id
    scope_owner_id, include_global = access.GLOBAL_ONLY
    logger.info(
        "POST /employee/chat/stream query=%r top_k=%s conversation_id=%s employee_id=%s",
        payload.query,
        payload.top_k,
        payload.conversation_id,
        owner_id,
    )

    def event_generator() -> Iterator[str]:
        for kind, value in chat_service.stream_answer(
            question=payload.query,
            top_k=payload.top_k,
            history=history,
            conversation_id=payload.conversation_id,
            owner_id=owner_id,
            retrieval_owner_id=scope_owner_id,
            include_global=include_global,
            show_global=True,
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


@router.post(
    "/chat/conversations",
    response_model=ConversationOut,
    status_code=201,
)
def employee_create_conversation(
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> ConversationOut:
    """Start a fresh, empty conversation owned by the resolved employee."""
    conversation = chat_service.create_conversation(db, owner_id=employee.id)
    return ConversationOut.model_validate(conversation)


@router.get("/chat/conversations", response_model=ConversationList)
def employee_list_conversations(
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> ConversationList:
    """List the resolved employee's own ACTIVE conversations, newest first."""
    conversations = chat_service.list_conversations(db, owner_id=employee.id)
    items = [ConversationOut.model_validate(c) for c in conversations]
    return ConversationList(items=items, total=len(items))


@router.get("/chat/conversations/{conversation_id}", response_model=ConversationDetail)
def employee_get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> ConversationDetail:
    """Return one of the employee's own ACTIVE conversations with its messages.

    Raises 404 (via ``NotFoundError``) when the conversation is missing/deleted or
    owned by another employee/user (no existence leak).
    """
    conversation = chat_service.get_conversation_or_404(
        db, conversation_id, owner_id=employee.id
    )
    messages = chat_service.get_conversation_messages(db, conversation_id)
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        added_time=conversation.added_time,
        last_modified=conversation.last_modified,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.delete("/chat/conversations/{conversation_id}", response_model=MessageResponse)
def employee_delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> MessageResponse:
    """Soft-delete one of the employee's own conversations (and its messages)."""
    chat_service.soft_delete_conversation(db, conversation_id, owner_id=employee.id)
    return MessageResponse(detail=f"Conversation {conversation_id} deleted")


@router.get("/auth/me", response_model=EmployeeMeResponse)
def employee_me(
    request: Request,
    employee: User = Depends(get_current_employee),
) -> EmployeeMeResponse:
    """Return the resolved employee identity backing the current request.

    ``id``/``email``/``role`` come from the provisioned backing user; ``emp_id``/
    ``full_name`` echo the optional, unverified ``X-Employee-Id``/
    ``X-Employee-Name`` headers (not persisted on the user row yet).
    """
    return EmployeeMeResponse(
        id=employee.id,
        email=employee.email,
        role=employee.role,
        emp_id=request.headers.get(_HEADER_EMPLOYEE_ID),
        full_name=request.headers.get(_HEADER_EMPLOYEE_NAME),
    )
