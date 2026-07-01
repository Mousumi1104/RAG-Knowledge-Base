"""Employee Zone chat API routes (no authentication, current mode).

Mirrors the user-facing :mod:`app.api.chat` router but for Employee Zone
employees, who read ONLY the admin-curated **global** knowledge base — never any
private/personal documents. Key differences from ``/chat``:

- Identity is resolved by :func:`app.core.employee_zone.resolve_employee` instead
  of a bearer token; in the current mode this requires NO token (optional,
  unverified ``X-Employee-*`` headers select/JIT-provision the backing user).
- Every retrieval uses the server-derived global-only scope and surfaces global
  company-doc citations.
- **Chat history lives in its OWN tables** (``employee_conversations`` /
  ``employee_messages``, see :mod:`app.services.employee_chat_service`), owned by
  a string ``employee_id`` (the ``X-Employee-Id`` header, falling back to the
  resolved email). Turns are auto-persisted; the history endpoints are a
  read/manage layer over them.
- There is intentionally NO employee upload/document endpoint.

Endpoints:
- ``POST   /employee/chat/query``               — non-streaming grounded answer + persistence.
- ``POST   /employee/chat/stream``              — SSE token stream + persistence.
- ``POST   /employee/chat/conversations``       — start a fresh empty conversation.
- ``GET    /employee/chat/conversations``       — list the employee's conversations (paginated).
- ``GET    /employee/chat/conversations/{id}``  — one conversation with messages.
- ``PATCH  /employee/chat/conversations/{id}``  — rename a conversation.
- ``DELETE /employee/chat/conversations/{id}``  — soft-delete a conversation (idempotent).
- ``GET    /employee/auth/me``                  — the resolved employee identity.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.employee_zone import (
    _HEADER_EMPLOYEE_ID,
    _HEADER_EMPLOYEE_NAME,
    require_api_key,
    resolve_employee,
)
from app.database.session import get_db
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse, Source
from app.schemas.employee import EmployeeMeResponse
from app.schemas.employee_history import (
    ConversationCreateRequest,
    ConversationDeleteResponse,
    ConversationRenameRequest,
    EmployeeConversationDetail,
    EmployeeConversationListResponse,
    EmployeeConversationSummary,
)
from app.services import employee_chat_service
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/employee",
    tags=["employee"],
    # Shared API-key gate on every /employee/* route. Fail-open while
    # EZ_CHAT_API_KEY is unset (rollout window); see EZ chat auth spec §2.
    dependencies=[Depends(require_api_key)],
)

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
    The backing user gives us a reliable email for the history owner key.
    """
    return resolve_employee(db, request)


def _owner_identity(request: Request, employee: User) -> tuple[str, str | None, str | None]:
    """Resolve the ``(employee_id, email, name)`` used for history ownership."""
    return employee_chat_service.resolve_owner(
        employee_id_header=request.headers.get(_HEADER_EMPLOYEE_ID),
        email=employee.email,
        name=request.headers.get(_HEADER_EMPLOYEE_NAME),
    )


@router.post("/chat/query", response_model=ChatResponse)
def employee_chat_query(
    payload: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> ChatResponse:
    """Answer a question with RAG over the GLOBAL company knowledge base only.

    Retrieval is pinned to the global-only scope; the turn is auto-persisted to
    the employee's own chat-history conversation.
    """
    history = [message.model_dump() for message in payload.history] if payload.history else None
    employee_id, email, name = _owner_identity(request, employee)
    logger.info(
        "POST /employee/chat/query query=%r top_k=%s conversation_id=%s employee_id=%r",
        payload.query,
        payload.top_k,
        payload.conversation_id,
        employee_id,
    )

    result = employee_chat_service.answer(
        db,
        question=payload.query,
        top_k=payload.top_k,
        history=history,
        conversation_id=payload.conversation_id,
        employee_id=employee_id,
        email=email,
        name=name,
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
    request: Request,
    employee: User = Depends(get_current_employee),
) -> StreamingResponse:
    """Stream a grounded answer (global-only scope) as Server-Sent Events.

    The turn is auto-persisted to the employee's own chat-history conversation.
    """
    history = [message.model_dump() for message in payload.history] if payload.history else None
    # Capture identity as plain strings: the SSE generator runs AFTER the
    # request-scoped DB session closes (answer_stream owns its own session).
    employee_id, email, name = _owner_identity(request, employee)
    logger.info(
        "POST /employee/chat/stream query=%r top_k=%s conversation_id=%s employee_id=%r",
        payload.query,
        payload.top_k,
        payload.conversation_id,
        employee_id,
    )

    def event_generator() -> Iterator[str]:
        for kind, value in employee_chat_service.answer_stream(
            question=payload.query,
            top_k=payload.top_k,
            history=history,
            conversation_id=payload.conversation_id,
            employee_id=employee_id,
            email=email,
            name=name,
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
    response_model=EmployeeConversationSummary,
    status_code=201,
)
def employee_create_conversation(
    request: Request,
    payload: ConversationCreateRequest | None = None,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> EmployeeConversationSummary:
    """Start a fresh, empty conversation owned by the resolved employee."""
    employee_id, email, name = _owner_identity(request, employee)
    conv = employee_chat_service.create_conversation(
        db,
        employee_id=employee_id,
        email=email,
        name=name,
        title=payload.title if payload else None,
    )
    return EmployeeConversationSummary(
        id=conv.id,
        title=conv.title,
        created_at=conv.added_time,
        updated_at=conv.last_modified,
        message_count=conv.message_count or 0,
        preview=None,
    )


@router.get("/chat/conversations", response_model=EmployeeConversationListResponse)
def employee_list_conversations(
    request: Request,
    limit: int = 20,
    cursor: str | None = None,
    offset: int | None = None,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> EmployeeConversationListResponse:
    """List the resolved employee's ACTIVE conversations, newest first, paginated."""
    employee_id, _email, _name = _owner_identity(request, employee)
    return employee_chat_service.list_conversations(
        db, employee_id=employee_id, limit=limit, cursor=cursor, offset=offset
    )


@router.get("/chat/conversations/{conversation_id}", response_model=EmployeeConversationDetail)
def employee_get_conversation(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> EmployeeConversationDetail:
    """Return one of the employee's ACTIVE conversations with its messages.

    Raises 404 (via ``NotFoundError``) when the conversation is missing/deleted or
    owned by another employee (no existence leak / 404-mask).
    """
    employee_id, _email, _name = _owner_identity(request, employee)
    return employee_chat_service.get_conversation(
        db, conversation_id=conversation_id, employee_id=employee_id
    )


@router.patch("/chat/conversations/{conversation_id}", response_model=EmployeeConversationDetail)
def employee_rename_conversation(
    conversation_id: int,
    payload: ConversationRenameRequest,
    request: Request,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> EmployeeConversationDetail:
    """Rename a conversation. Empty/whitespace title -> 400; 404-mask on foreign."""
    employee_id, _email, _name = _owner_identity(request, employee)
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    return employee_chat_service.rename_conversation(
        db, conversation_id=conversation_id, employee_id=employee_id, title=title
    )


@router.delete("/chat/conversations/{conversation_id}", response_model=ConversationDeleteResponse)
def employee_delete_conversation(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    employee: User = Depends(get_current_employee),
) -> ConversationDeleteResponse:
    """Idempotent soft-delete of the employee's conversation (+ its messages)."""
    employee_id, _email, _name = _owner_identity(request, employee)
    return employee_chat_service.delete_conversation(
        db, conversation_id=conversation_id, employee_id=employee_id
    )


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
