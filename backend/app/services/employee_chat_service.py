"""Employee-Zone chat history service.

Owns the SEPARATE ``employee_conversations`` / ``employee_messages`` tables,
keyed by a string ``employee_id`` owner. It provides:

- conversation CRUD: create, list (cursor+offset paginated, with preview +
  message_count), get-with-messages, rename, idempotent soft-delete, all scoped
  to the caller's ``employee_id`` (foreign/missing -> 404-mask, matching the
  admin/user convention);
- ``answer`` / ``answer_stream``: RAG turns that reuse
  :mod:`app.services.chat_service` retrieval + generation (global-only scope,
  graceful provider-error degradation, small-talk short-circuit) but persist the
  turn to the employee tables under the caller's conversation.

Identity is Mode-A (unverified headers) for now; ownership is only as trustworthy
as the shared API key. ``employee_id`` is the ``X-Employee-Id`` header, falling
back to the resolved employee email when that header is absent.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core import access
from app.database.base import Status
from app.database.session import SessionLocal
from app.models.employee_conversation import EmployeeConversation
from app.models.employee_message import EmployeeMessage
from app.schemas.employee_history import (
    ConversationDeleteResponse,
    EmployeeConversationDetail,
    EmployeeConversationListResponse,
    EmployeeConversationSummary,
    EmployeeMessageOut,
)
from app.services import chat_service as cs
from app.utils.errors import NotFoundError, ProviderError, RateLimitError
from app.utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MAX_TITLE_LEN = 200
_PREVIEW_CHARS = 80

# Leading identity preamble the EZ app prepends to the query
# (``(For context, I am <name> ...)``); stripped before persisting user content
# so history reads cleanly. Retrieval still uses the full query text.
_PREAMBLE_RE = re.compile(r"^\s*\(\s*for context, i am\b.*?\)\s*", re.IGNORECASE | re.DOTALL)


# ---------------------------------------------------------------------------
# Identity / helpers
# ---------------------------------------------------------------------------
def resolve_owner(
    *, employee_id_header: str | None, email: str | None, name: str | None
) -> tuple[str, str | None, str | None]:
    """Resolve the ``(owner_key, email, name)`` for history ownership.

    Owner key is the ``X-Employee-Id`` header when present, else the resolved
    employee email (always available via the Mode-A default). Raises if neither
    is available (should not happen once identity is resolved).
    """
    owner = (employee_id_header or "").strip() or (email or "").strip()
    if not owner:
        raise NotFoundError("Employee identity could not be resolved")
    return owner, (email or None), (name or None)


def _strip_preamble(text: str) -> str:
    return _PREAMBLE_RE.sub("", text or "", count=1).strip()


def _preview(text: str | None) -> str | None:
    if not text:
        return None
    snippet = _strip_preamble(text).strip()
    if len(snippet) <= _PREVIEW_CHARS:
        return snippet or None
    return snippet[:_PREVIEW_CHARS].rstrip() + "…"


def _encode_cursor(last_modified: datetime, conv_id: int) -> str:
    raw = json.dumps({"lm": last_modified.isoformat(), "id": conv_id})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        data = json.loads(raw)
        return datetime.fromisoformat(data["lm"]), int(data["id"])
    except Exception:  # noqa: BLE001 - a bad cursor just resets to the first page
        return None


# ---------------------------------------------------------------------------
# Conversation lookups (ownership-scoped)
# ---------------------------------------------------------------------------
def _get_owned_active(db: Session, conversation_id: int, employee_id: str) -> EmployeeConversation | None:
    stmt = select(EmployeeConversation).where(
        EmployeeConversation.id == conversation_id,
        EmployeeConversation.employee_id == employee_id,
        EmployeeConversation.status == Status.ACTIVE.value,
    )
    return db.execute(stmt).scalar_one_or_none()


def _get_owned_or_404(db: Session, conversation_id: int, employee_id: str) -> EmployeeConversation:
    conv = _get_owned_active(db, conversation_id, employee_id)
    if conv is None:
        # 404-mask: never reveal that a foreign conversation exists.
        raise NotFoundError(f"Conversation {conversation_id} not found")
    return conv


def _load_history(db: Session, conversation_id: int) -> list[dict[str, str]]:
    """Prior turns for follow-up support, oldest->newest, as role/content dicts."""
    stmt = (
        select(EmployeeMessage.role, EmployeeMessage.content)
        .where(
            EmployeeMessage.conversation_id == conversation_id,
            EmployeeMessage.status == Status.ACTIVE.value,
        )
        .order_by(EmployeeMessage.id.asc())
    )
    return [{"role": role, "content": content} for role, content in db.execute(stmt).all()]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def create_conversation(
    db: Session,
    *,
    employee_id: str,
    email: str | None,
    name: str | None,
    title: str | None = None,
) -> EmployeeConversation:
    conv = EmployeeConversation(
        employee_id=employee_id,
        employee_email=email,
        employee_name=name,
        title=cs._derive_title(title or ""),
        message_count=0,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _resolve_or_create(
    db: Session,
    *,
    employee_id: str,
    email: str | None,
    name: str | None,
    question: str,
    conversation_id: int | None,
) -> EmployeeConversation:
    """Reuse the caller's own ACTIVE conversation, else create a new one.

    A provided id that is missing/foreign falls through to creating a NEW
    conversation (never attaches a turn to another employee's thread).
    """
    if conversation_id is not None:
        existing = _get_owned_active(db, conversation_id, employee_id)
        if existing is not None:
            return existing
    conv = EmployeeConversation(
        employee_id=employee_id,
        employee_email=email,
        employee_name=name,
        title=cs._derive_title(question),
        message_count=0,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _save_turn(
    db: Session,
    conv: EmployeeConversation,
    *,
    user_text: str,
    assistant_text: str,
    sources: list[dict] | None,
) -> tuple[EmployeeMessage, EmployeeMessage]:
    """Persist the user + assistant messages and bump the conversation counters."""
    user_msg = EmployeeMessage(
        conversation_id=conv.id,
        employee_id=conv.employee_id,
        role="user",
        content=user_text,
        sources=None,
    )
    assistant_msg = EmployeeMessage(
        conversation_id=conv.id,
        employee_id=conv.employee_id,
        role="assistant",
        content=assistant_text,
        sources=sources or None,
    )
    db.add(user_msg)
    db.add(assistant_msg)
    # Bump count; touching the row also refreshes last_modified (mixin onupdate).
    conv.message_count = (conv.message_count or 0) + 2
    conv.last_modified = func.now()
    db.commit()
    db.refresh(assistant_msg)
    return user_msg, assistant_msg


# ---------------------------------------------------------------------------
# Read / manage API
# ---------------------------------------------------------------------------
def list_conversations(
    db: Session,
    *,
    employee_id: str,
    limit: int | None = None,
    cursor: str | None = None,
    offset: int | None = None,
) -> EmployeeConversationListResponse:
    """Newest-first, cursor-paginated list of the caller's conversations."""
    limit = max(1, min(limit or _DEFAULT_LIMIT, _MAX_LIMIT))

    stmt = select(EmployeeConversation).where(
        EmployeeConversation.employee_id == employee_id,
        EmployeeConversation.status == Status.ACTIVE.value,
    )
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        lm, cid = decoded
        stmt = stmt.where(
            or_(
                EmployeeConversation.last_modified < lm,
                and_(
                    EmployeeConversation.last_modified == lm,
                    EmployeeConversation.id < cid,
                ),
            )
        )
    stmt = stmt.order_by(
        EmployeeConversation.last_modified.desc(), EmployeeConversation.id.desc()
    )
    # Offset is an alternative to the (canonical) cursor when no cursor is given.
    if decoded is None and offset:
        stmt = stmt.offset(max(0, offset))
    stmt = stmt.limit(limit + 1)

    rows = list(db.execute(stmt).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    previews = _latest_message_previews(db, [r.id for r in rows])
    conversations = [
        EmployeeConversationSummary(
            id=r.id,
            title=r.title,
            created_at=r.added_time,
            updated_at=r.last_modified,
            message_count=r.message_count or 0,
            preview=previews.get(r.id),
        )
        for r in rows
    ]
    next_cursor = (
        _encode_cursor(rows[-1].last_modified, rows[-1].id) if has_more and rows else None
    )
    return EmployeeConversationListResponse(
        conversations=conversations, next_cursor=next_cursor, has_more=has_more
    )


def _latest_message_previews(db: Session, conv_ids: Sequence[int]) -> dict[int, str | None]:
    """Map each conversation id -> a short preview of its latest message."""
    if not conv_ids:
        return {}
    latest = (
        select(
            EmployeeMessage.conversation_id.label("cid"),
            func.max(EmployeeMessage.id).label("max_id"),
        )
        .where(
            EmployeeMessage.conversation_id.in_(conv_ids),
            EmployeeMessage.status == Status.ACTIVE.value,
        )
        .group_by(EmployeeMessage.conversation_id)
        .subquery()
    )
    stmt = select(EmployeeMessage.conversation_id, EmployeeMessage.content).join(
        latest, EmployeeMessage.id == latest.c.max_id
    )
    return {cid: _preview(content) for cid, content in db.execute(stmt).all()}


def get_conversation(db: Session, *, conversation_id: int, employee_id: str) -> EmployeeConversationDetail:
    conv = _get_owned_or_404(db, conversation_id, employee_id)
    stmt = (
        select(EmployeeMessage)
        .where(
            EmployeeMessage.conversation_id == conversation_id,
            EmployeeMessage.status == Status.ACTIVE.value,
        )
        .order_by(EmployeeMessage.id.asc())
    )
    messages = db.execute(stmt).scalars().all()
    return EmployeeConversationDetail(
        id=conv.id,
        title=conv.title,
        created_at=conv.added_time,
        updated_at=conv.last_modified,
        message_count=conv.message_count or 0,
        messages=[
            EmployeeMessageOut(
                id=m.id,
                conversation_id=m.conversation_id,
                role=m.role,
                content=m.content,
                sources=m.sources,
                created_at=m.added_time,
            )
            for m in messages
        ],
    )


def rename_conversation(
    db: Session, *, conversation_id: int, employee_id: str, title: str
) -> EmployeeConversationDetail:
    """Rename the conversation title. ``title`` MUST be pre-validated non-empty."""
    conv = _get_owned_or_404(db, conversation_id, employee_id)
    conv.title = title.strip()[:_MAX_TITLE_LEN]
    db.commit()
    db.refresh(conv)
    return EmployeeConversationDetail(
        id=conv.id,
        title=conv.title,
        created_at=conv.added_time,
        updated_at=conv.last_modified,
        message_count=conv.message_count or 0,
        messages=[],
    )


def delete_conversation(
    db: Session, *, conversation_id: int, employee_id: str
) -> ConversationDeleteResponse:
    """Idempotent soft-delete: unknown/foreign ids still return success."""
    conv = _get_owned_active(db, conversation_id, employee_id)
    if conv is not None:
        conv.status = Status.DELETED.value
        for msg in db.execute(
            select(EmployeeMessage).where(
                EmployeeMessage.conversation_id == conversation_id,
                EmployeeMessage.status == Status.ACTIVE.value,
            )
        ).scalars():
            msg.status = Status.DELETED.value
        db.commit()
    return ConversationDeleteResponse(deleted=True, id=conversation_id)


# ---------------------------------------------------------------------------
# RAG turns (auto-persist to employee tables)
# ---------------------------------------------------------------------------
def answer(
    db: Session,
    *,
    question: str,
    top_k: int | None,
    history: Sequence[Mapping[str, str]] | None,
    conversation_id: int | None,
    employee_id: str,
    email: str | None,
    name: str | None,
) -> dict:
    """Answer over the GLOBAL KB and persist the turn under the employee's thread."""
    cleaned = cs._sanitize_question(question)
    stored_user = _strip_preamble(cleaned) or cleaned
    conv = _resolve_or_create(
        db,
        employee_id=employee_id,
        email=email,
        name=name,
        # Title derives from the preamble-stripped text so history reads cleanly.
        question=stored_user or (question or ""),
        conversation_id=conversation_id,
    )

    if not cleaned:
        _, amsg = _save_turn(
            db, conv, user_text=question or "", assistant_text=cs._NO_CONTEXT_ANSWER, sources=[]
        )
        return _result(conv.id, cs._NO_CONTEXT_ANSWER, [], amsg.id)

    if cs._is_smalltalk(cleaned):
        _, amsg = _save_turn(
            db, conv, user_text=stored_user, assistant_text=cs._SMALLTALK_ANSWER, sources=[]
        )
        return _result(conv.id, cs._SMALLTALK_ANSWER, [], amsg.id)

    effective_history = _load_history(db, conv.id) or history
    scope_owner, include_global = access.GLOBAL_ONLY
    sources: list[dict] = []
    try:
        system, user, sources = cs._retrieve_and_build_prompt(
            db,
            cleaned,
            top_k,
            effective_history,
            scope_owner,
            include_global=include_global,
            show_global=True,
            employee_name=name,
            employee_id=employee_id,
        )
        ans = cs._generate_answer(system, user)
    except (RateLimitError, ProviderError) as exc:
        logger.error("Employee provider failure: %s; returning graceful message.", exc)
        ans, sources = cs._SERVICE_BUSY_ANSWER, []
    except Exception as exc:  # noqa: BLE001 - never 500 the employee chat endpoint
        logger.exception("Unexpected employee chat failure: %s; graceful message.", exc)
        ans, sources = cs._SERVICE_BUSY_ANSWER, []

    if not ans or not ans.strip():
        ans = cs._NO_CONTEXT_ANSWER
    if sources and cs._is_no_answer(ans):
        sources = []

    _, amsg = _save_turn(db, conv, user_text=stored_user, assistant_text=ans, sources=sources)
    return _result(conv.id, ans, sources, amsg.id)


def _result(conversation_id: int, answer_text: str, sources: list[dict], message_id: int) -> dict:
    return {
        "answer": answer_text,
        "sources": sources,
        "conversation_id": conversation_id,
        "message_id": message_id,
    }


def answer_stream(
    *,
    question: str,
    top_k: int | None,
    history: Sequence[Mapping[str, str]] | None,
    conversation_id: int | None,
    employee_id: str,
    email: str | None,
    name: str | None,
) -> Iterator[tuple[str, object]]:
    """Streaming variant of :func:`answer`. Owns its own session; never raises.

    Yields ``("delta", text)`` frames, one terminal ``("done", {...})`` frame, and
    degrades to a rendered ``_SERVICE_BUSY_ANSWER`` (never a bare error frame) so
    the client always shows something.
    """
    db = SessionLocal()
    conv_id: int | None = None
    streamed = False
    try:
        cleaned = cs._sanitize_question(question)
        stored_user = _strip_preamble(cleaned) or cleaned
        conv = _resolve_or_create(
            db,
            employee_id=employee_id,
            email=email,
            name=name,
            # Title derives from the preamble-stripped text so history reads cleanly.
            question=stored_user or (question or ""),
            conversation_id=conversation_id,
        )
        conv_id = conv.id

        if not cleaned:
            _, amsg = _save_turn(
                db, conv, user_text=question or "", assistant_text=cs._NO_CONTEXT_ANSWER, sources=[]
            )
            yield ("delta", cs._NO_CONTEXT_ANSWER)
            yield ("done", {"sources": [], "conversation_id": conv_id, "message_id": amsg.id})
            return

        if cs._is_smalltalk(cleaned):
            _, amsg = _save_turn(
                db, conv, user_text=stored_user, assistant_text=cs._SMALLTALK_ANSWER, sources=[]
            )
            yield ("delta", cs._SMALLTALK_ANSWER)
            yield ("done", {"sources": [], "conversation_id": conv_id, "message_id": amsg.id})
            return

        effective_history = _load_history(db, conv.id) or history
        scope_owner, include_global = access.GLOBAL_ONLY
        try:
            system, user, sources = cs._retrieve_and_build_prompt(
                db,
                cleaned,
                top_k,
                effective_history,
                scope_owner,
                include_global=include_global,
                show_global=True,
                employee_name=name,
                employee_id=employee_id,
            )
        except (RateLimitError, ProviderError) as exc:
            logger.error("Employee stream provider failure: %s; graceful message.", exc)
            _, amsg = _save_turn(
                db, conv, user_text=stored_user, assistant_text=cs._SERVICE_BUSY_ANSWER, sources=[]
            )
            yield ("delta", cs._SERVICE_BUSY_ANSWER)
            yield ("done", {"sources": [], "conversation_id": conv_id, "message_id": amsg.id})
            return

        chunks: list[str] = []
        for delta in cs._stream_answer_deltas(system, user):
            if not delta:
                continue
            chunks.append(delta)
            streamed = True
            yield ("delta", delta)

        full_answer = "".join(chunks) or cs._NO_CONTEXT_ANSWER
        if sources and cs._is_no_answer(full_answer):
            sources = []
        _, amsg = _save_turn(db, conv, user_text=stored_user, assistant_text=full_answer, sources=sources)
        yield (
            "done",
            {"sources": sources, "conversation_id": conv_id, "message_id": amsg.id},
        )
    except Exception as exc:  # noqa: BLE001 - never raise out of the SSE generator.
        logger.error("employee answer_stream failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        if not streamed:
            yield ("delta", cs._SERVICE_BUSY_ANSWER)
        yield ("done", {"sources": [], "conversation_id": conv_id, "message_id": None})
    finally:
        db.close()
