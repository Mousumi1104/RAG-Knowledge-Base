"""RAG chat business logic.

The flow is:

1. Sanitize the incoming question (strip, length-limit).
2. Retrieve the most relevant ACTIVE chunks via ``search_service.search``.
3. Build a strictly-grounded prompt from those chunks (and optional history).
4. Call the configured LLM provider to generate an answer.
5. Return ``{answer, sources}`` where every source is traceable back to a
   document/chunk that actually contributed context.

Provider robustness: if the configured cloud provider raises a
``RateLimitError``/``ProviderError`` (or any unexpected error), we fall back to
the deterministic local extractive provider so the endpoint still returns a
grounded answer instead of a 500.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.base import Status
from app.database.session import SessionLocal
from app.models.conversation import Conversation
from app.models.message import Message
from app.rag.llm import LocalExtractiveLLMProvider, get_llm_provider
from app.rag.prompt import build_rag_prompt
from app.services import search_service
from app.utils.errors import NotFoundError, ProviderError, RateLimitError
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Defensive cap on question length to bound prompt size and abuse.
_MAX_QUESTION_CHARS = 4000

# Max length for an auto-derived conversation title (from the first user message).
_MAX_TITLE_CHARS = 80

_NO_CONTEXT_ANSWER = (
    "I couldn't find relevant information in the knowledge base to answer your " "question."
)

# Phrases that mark an answer as a "not found"/disclaimer reply. Matched against
# the answer normalized exactly like ``_is_smalltalk`` does (lowercased,
# punctuation -> spaces, whitespace collapsed) so e.g. "couldn't" -> "couldn t".
# Kept specific to avoid false positives on normal grounded answers.
_NO_ANSWER_PHRASES: frozenset[str] = frozenset(
    {
        "could not find",
        "couldn t find",
        "not in the knowledge base",
        "no relevant information",
        "could not find the information",
        "could not find relevant information",
    }
)


def _is_no_answer(answer: str) -> bool:
    """Return ``True`` when ``answer`` is a not-found/disclaimer reply.

    Normalizes the answer the same way :func:`_is_smalltalk` normalizes a
    question (lowercase, punctuation stripped to spaces, whitespace collapsed)
    so contractions like "couldn't" become "couldn t", then returns ``True`` if
    the normalized text contains any phrase from :data:`_NO_ANSWER_PHRASES`.
    """
    if not answer:
        return False
    norm = re.sub(r"[^a-z0-9\s]", " ", answer.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return False
    return any(phrase in norm for phrase in _NO_ANSWER_PHRASES)

# Minimum retrieval similarity for a chunk to count as usable context. Weakly
# matching hits are dropped so they neither pollute the prompt nor surface as
# irrelevant source chips. Real questions' best chunk scores well above this.
# Calibrated for the local hashing embedder; revisit if the embedder changes.
# Filtered against the BLENDED hybrid ``rank_score`` (vector + lexical-overlap +
# definition bonus), not the raw vector score, so a chunk the lexical rescue
# legitimately reordered to the top also survives this gate.
#
# Restored to 0.1 (from a brief 0.05 over-reach): because the gate now reads the
# BLENDED rank_score, even the weakest genuinely-relevant chunk — the paraphrased
# CDN-provider headline chunk, blended ~0.29-0.37 — clears 0.1 comfortably. The
# 0.05 floor only admitted low-relevance noise chunks from UNRELATED documents
# into the LLM context, which the model then parroted. Off-topic exclusion when a
# strong hit exists is handled by the RELATIVE floor below, not by lowering this.
_MIN_RELEVANCE_SCORE = 0.1

# Relative (doc-agnostic) relevance floor applied AFTER the absolute gate, so an
# off-topic chunk from an unrelated document cannot ride into the LLM context
# alongside a strong on-topic hit. We keep only chunks whose blended rank_score
# is at least this FRACTION of the top surviving hit's rank_score. The local
# IDF-free embedder assigns big unrelated documents spuriously-high vector scores
# that land just below the genuine top hit; measured on the live multi-doc corpus
# (CDN doc + linear-algebra + statistics globals), real contamination chunks sit
# at ~0.6-0.95x the top hit while the top hit is reliably the correct on-topic
# chunk. A high fraction is therefore required to separate them; lower fractions
# (0.4-0.6) let the noise through. The fraction is relative, so it scales with
# answer confidence: a high-scoring exact hit keeps only its near-equals, while a
# weaker hit still keeps its own near-equals (recall preserved -- the gold chunk
# is always the top hit, so it always survives its own floor).
_RELATIVE_RELEVANCE_FRACTION = 0.9

# Hard cap on how many chunks may enter the LLM context. Even within the relative
# floor, a long tail of near-equal chunks dilutes the prompt; the answer lives in
# the top few. Bounds prompt size and further limits any residual contamination.
_MAX_CONTEXT_CHUNKS = 3

# Friendly, brand-neutral reply for greetings/small talk. No sources attached.
_SMALLTALK_ANSWER = (
    "Hi! I'm a knowledge-base Q&A assistant. I can answer questions using the "
    "documents in the knowledge base. What would you like to know?"
)

# Short, bare greeting/identity/small-talk tokens. Matched only against the FULL
# normalized message (so they cannot be substring-matched inside a real question).
_SMALLTALK_PHRASES: frozenset[str] = frozenset(
    {
        "hi",
        "hii",
        "hiii",
        "hello",
        "helo",
        "hey",
        "heya",
        "hey there",
        "hi there",
        "hello there",
        "yo",
        "sup",
        "whats up",
        "what s up",
        "good morning",
        "good afternoon",
        "good evening",
        "good day",
        "greetings",
        "how are you",
        "how are you doing",
        "how r u",
        "how are u",
        "hows it going",
        "how is it going",
        "how do you do",
        "who are you",
        "what are you",
        "what is your name",
        "what s your name",
        "whats your name",
        "what can you do",
        "what do you do",
        "help",
        "thanks",
        "thank you",
        "thank you so much",
        "thanks a lot",
        "thx",
        "ty",
        "cheers",
        "bye",
        "goodbye",
        "good bye",
        "see you",
        "see ya",
        "cya",
        "ok thanks",
        "okay thanks",
        "ok thank you",
        "nice to meet you",
    }
)

# Max word count for a message to even be considered small talk. Real questions
# tend to be longer; this guards against swallowing genuine queries that happen
# to start with a greeting word.
_SMALLTALK_MAX_WORDS = 6


def _is_smalltalk(question: str) -> bool:
    """Return ``True`` for short greetings/identity/thanks/farewell small talk.

    STRICT by design: matches only when the *entire* normalized message (lower
    cased, punctuation stripped) is a known whole-phrase greeting. It never
    substring-matches, so real questions such as
    ``"what is the transpose of a matrix?"`` return ``False`` even though they
    begin with ``"what is"``.
    """
    if not question:
        return False
    # Normalize: lowercase, strip punctuation to spaces, collapse whitespace.
    normalized = re.sub(r"[^a-z0-9\s]", " ", question.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False
    # Guard: anything beyond a short phrase is treated as a real message.
    if len(normalized.split()) > _SMALLTALK_MAX_WORDS:
        return False
    return normalized in _SMALLTALK_PHRASES


def _persist_smalltalk_turn(db: Session, conv_id: int, cleaned: str) -> Message:
    """Persist the user + assistant messages for a small-talk turn (sources=[])."""
    _save_message(db, conv_id, "user", cleaned)
    return _save_message(db, conv_id, "assistant", _SMALLTALK_ANSWER, sources=[])


def _sanitize_question(question: str) -> str:
    """Strip and length-limit the raw user question."""
    cleaned = (question or "").strip()
    if len(cleaned) > _MAX_QUESTION_CHARS:
        cleaned = cleaned[:_MAX_QUESTION_CHARS].rstrip()
    return cleaned


def _select_context(hits: Sequence[Mapping[str, object]]) -> list[dict]:
    """Select the chunks that may ground the answer (precision-preserving).

    Applies, in order:

    1. ABSOLUTE gate: drop any hit whose blended ``rank_score`` (fallback raw
       ``score``) is below :data:`_MIN_RELEVANCE_SCORE`.
    2. RELATIVE floor: of the survivors, keep only those at least
       :data:`_RELATIVE_RELEVANCE_FRACTION` of the top survivor's rank_score, so
       an off-topic chunk from an unrelated document cannot ride into context
       next to a strong on-topic hit.
    3. CAP: keep at most :data:`_MAX_CONTEXT_CHUNKS` chunks.

    Hits are assumed already ordered most-relevant first by ``search_service``.
    Tenant isolation and the per-document cap are enforced upstream in
    ``search_service``; this only trims for relevance, never widens the set.
    """
    gated = [
        hit
        for hit in hits
        if float(hit.get("rank_score", hit.get("score", 0.0)) or 0.0) >= _MIN_RELEVANCE_SCORE
    ]
    if not gated:
        return []
    top_score = float(gated[0].get("rank_score", gated[0].get("score", 0.0)) or 0.0)
    relative_floor = _RELATIVE_RELEVANCE_FRACTION * top_score
    selected = [
        hit
        for hit in gated
        if float(hit.get("rank_score", hit.get("score", 0.0)) or 0.0) >= relative_floor
    ]
    return selected[:_MAX_CONTEXT_CHUNKS]


def _build_sources(hits: Sequence[Mapping[str, object]], owner_id: int) -> list[dict]:
    """Shape search hits into API source dicts: ONLY the user's OWN documents.

    Source-reference rule (security-relevant): a hit is shown as a source iff it
    is one of the requesting user's own documents — ``owner_id == <owner_id> AND
    is_global == False``. Admin-global docs (``is_global == True``) are allowed to
    ground the answer (they stay in the prompt context built from ``hits``) but
    are EXCLUDED from the returned sources. If the answer was grounded only on
    global docs, this returns an empty list.

    Retrieval order (most-relevant first) is preserved. Exact ``(document,
    excerpt)`` duplicates are dropped. No numeric score is emitted.
    """
    seen: set[tuple[int, str]] = set()
    sources: list[dict] = []
    for hit in hits:
        # Display only the user's own (non-global) documents.
        hit_owner_id = hit.get("owner_id")
        if hit.get("is_global") or hit_owner_id is None or int(hit_owner_id) != int(owner_id):
            continue
        document_id = int(hit.get("document_id", 0) or 0)
        excerpt = str(hit.get("excerpt") or "")
        key = (document_id, excerpt)
        if key in seen:
            continue
        seen.add(key)
        raw_page = hit.get("page")
        try:
            page = int(raw_page) if raw_page is not None else None
        except (TypeError, ValueError):
            page = None
        sources.append(
            {
                "document": str(hit.get("document_name") or "unknown document"),
                "excerpt": excerpt,
                "document_id": document_id,
                "page": page,
            }
        )
    return sources


def _generate_answer(system: str, user: str) -> str:
    """Generate an answer, falling back to the local provider on provider errors."""
    provider = get_llm_provider()
    try:
        return provider.generate(user, system=system)
    except (RateLimitError, ProviderError) as exc:
        logger.warning(
            "LLM provider failed (%s); falling back to local extractive provider.",
            exc,
        )
    except Exception as exc:  # noqa: BLE001 - never 500 on generation
        logger.error("Unexpected LLM provider error: %s; falling back to local.", exc)

    if isinstance(provider, LocalExtractiveLLMProvider):
        # Local provider already failed unexpectedly; return a safe message.
        return _NO_CONTEXT_ANSWER
    return LocalExtractiveLLMProvider().generate(user, system=system)


def _stream_answer_deltas(system: str, user: str) -> Iterator[str]:
    """Yield answer deltas, falling back to the local provider on provider errors.

    Mirrors :func:`_generate_answer` but for the streaming path. The selected
    provider's ``generate_stream`` already self-falls back to a local word
    stream on transport/rate-limit errors, so this is a second safety net for
    unexpected failures (e.g. provider construction). Never raises.
    """
    provider = get_llm_provider()
    try:
        yield from provider.generate_stream(user, system=system)
        return
    except (RateLimitError, ProviderError) as exc:
        logger.warning(
            "LLM provider stream failed (%s); falling back to local extractive provider.",
            exc,
        )
    except Exception as exc:  # noqa: BLE001 - never raise from the stream.
        logger.error("Unexpected LLM provider stream error: %s; falling back to local.", exc)

    if isinstance(provider, LocalExtractiveLLMProvider):
        # Local provider already failed; emit a safe single chunk.
        yield _NO_CONTEXT_ANSWER
        return
    try:
        yield from LocalExtractiveLLMProvider().generate_stream(user, system=system)
    except Exception as exc:  # noqa: BLE001 - never raise from the stream.
        logger.error("Local fallback stream failed: %s", exc)
        yield _NO_CONTEXT_ANSWER


# --- Conversation / message persistence -------------------------------------


def _derive_title(question: str) -> str:
    """Derive a short conversation title from the first user message."""
    cleaned = (question or "").strip().replace("\n", " ")
    if len(cleaned) > _MAX_TITLE_CHARS:
        cleaned = cleaned[:_MAX_TITLE_CHARS].rstrip() + "..."
    return cleaned or "New conversation"


def _get_active_conversation(
    db: Session, conversation_id: int, owner_id: int
) -> Conversation | None:
    """Return one ACTIVE conversation owned by ``owner_id`` by id, or ``None``.

    Scoped by ``user_id`` so a user can never resolve/attach to another user's
    thread (a foreign id is indistinguishable from a missing one).
    """
    stmt = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.status == Status.ACTIVE.value,
        Conversation.user_id == owner_id,
    )
    return db.execute(stmt).scalars().first()


def resolve_or_create_conversation(
    db: Session,
    conversation_id: int | None,
    question: str,
    owner_id: int,
) -> Conversation:
    """Resolve an existing ACTIVE conversation (owned by ``owner_id``) or create one.

    When ``conversation_id`` refers to an ACTIVE conversation OWNED BY ``owner_id``
    it is reused; otherwise (omitted, null, not found, or owned by another user) a
    new conversation is created with ``user_id = owner_id`` and a title derived
    from ``question``. Commits so the id is available. Never attaches to another
    user's thread.
    """
    if conversation_id is not None:
        conversation = _get_active_conversation(db, conversation_id, owner_id)
        if conversation is not None:
            return conversation
        logger.info(
            "conversation_id=%s not found/inactive/foreign; creating a new conversation.",
            conversation_id,
        )

    conversation = Conversation(title=_derive_title(question), user_id=owner_id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def create_conversation(db: Session, owner_id: int, title: str | None = None) -> Conversation:
    """Create a fresh empty conversation owned by ``owner_id`` and return it.

    Used by the explicit ``POST /chat/conversations`` endpoint so the client can
    start a brand-new chat (e.g. on login/session start) and receive its unique
    id up front, before the first message. ``user_id`` is always set to
    ``owner_id`` so the new thread belongs to the caller and to no one else.
    """
    conversation = Conversation(title=_derive_title(title or ""), user_id=owner_id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    logger.info("Created conversation id=%s for user_id=%s", conversation.id, owner_id)
    return conversation


def _load_history(
    db: Session, conversation_id: int, exclude_message_id: int | None = None
) -> list[dict]:
    """Load prior ACTIVE messages (chronological) as ``{role, content}`` dicts."""
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.status == Status.ACTIVE.value,
        )
        .order_by(Message.id.asc())
    )
    messages = db.execute(stmt).scalars().all()
    return [
        {"role": m.role, "content": m.content}
        for m in messages
        if exclude_message_id is None or m.id != exclude_message_id
    ]


def _save_message(
    db: Session,
    conversation_id: int,
    role: str,
    content: str,
    sources: list[dict] | None = None,
) -> Message:
    """Persist a single message and commit; returns the refreshed row."""
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        sources=sources,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def _retrieve_and_build_prompt(
    db: Session,
    cleaned_question: str,
    top_k: int | None,
    history: Sequence[Mapping[str, str]] | None,
    owner_id: int,
) -> tuple[str, str, list[dict]]:
    """Retrieve tenant-scoped context and build the grounded prompt.

    Retrieval is restricted (DEFAULT-DENY) to the user's own docs + admin-global
    docs. ``relevant`` (the prompt context) may include global docs that ground
    the answer; ``_build_sources`` then keeps ONLY the user's own docs as visible
    sources. Returns ``(system, user, sources)``.
    """
    hits = search_service.search(
        db, query=cleaned_question, top_k=top_k, owner_id=owner_id, include_global=True
    )
    # Select context on the blended hybrid ``rank_score``: an absolute relevance
    # gate, then a relative (fraction-of-top) floor so off-topic chunks from
    # unrelated docs can't ride into context beside a strong hit, then a cap.
    # (See :func:`_select_context`.) The lexical rescue that reorders a gold
    # chunk to the top means that chunk both clears the gate and anchors the
    # relative floor, so recall is preserved while contamination is excluded.
    relevant = _select_context(hits)
    logger.info(
        "Chat query=%r retrieved %s chunk(s), %s selected for context",
        cleaned_question,
        len(hits),
        len(relevant),
    )
    system, user = build_rag_prompt(cleaned_question, relevant, history=history)
    return system, user, _build_sources(relevant, owner_id)


def list_conversations(db: Session, owner_id: int) -> list[Conversation]:
    """Return the user's own ACTIVE conversations, newest first."""
    stmt = (
        select(Conversation)
        .where(
            Conversation.status == Status.ACTIVE.value,
            Conversation.user_id == owner_id,
        )
        .order_by(Conversation.last_modified.desc(), Conversation.id.desc())
    )
    return list(db.execute(stmt).scalars().all())


def get_conversation_or_404(db: Session, conversation_id: int, owner_id: int) -> Conversation:
    """Return one of the user's own ACTIVE conversations or raise :class:`NotFoundError`.

    A conversation owned by another user 404s identically to a missing one (no
    existence leak).
    """
    conversation = _get_active_conversation(db, conversation_id, owner_id)
    if conversation is None:
        raise NotFoundError(f"Conversation {conversation_id} not found")
    return conversation


def get_conversation_messages(db: Session, conversation_id: int) -> list[Message]:
    """Return ACTIVE messages of a conversation in chronological (id ASC) order.

    Callers MUST first establish ownership of ``conversation_id`` (e.g. via
    :func:`get_conversation_or_404`) so messages are only ever read for a
    conversation the user owns.
    """
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.status == Status.ACTIVE.value,
        )
        .order_by(Message.id.asc())
    )
    return list(db.execute(stmt).scalars().all())


def soft_delete_conversation(db: Session, conversation_id: int, owner_id: int) -> Conversation:
    """Soft-delete one of the user's own conversations (and its messages); 404 if missing."""
    conversation = get_conversation_or_404(db, conversation_id, owner_id)
    conversation.status = Status.DELETED.value
    db.add(conversation)
    for message in get_conversation_messages(db, conversation_id):
        message.status = Status.DELETED.value
        db.add(message)
    db.commit()
    logger.info("Soft-deleted conversation id=%s", conversation_id)
    return conversation


def stream_answer(
    question: str,
    top_k: int | None = None,
    history: Sequence[Mapping[str, str]] | None = None,
    conversation_id: int | None = None,
    *,
    owner_id: int,
) -> Iterator[tuple[str, object]]:
    """Stream a grounded answer while persisting the conversation turn.

    Owns its OWN ``SessionLocal()`` session (the request-scoped ``get_db``
    session is closed before a ``StreamingResponse`` body runs). ``owner_id`` is
    passed as a plain int (NOT an ORM ``User``) across that session boundary; it
    scopes both retrieval (own + global docs) and the conversation. Resolves/
    creates the conversation and persists the user message before streaming, then
    persists the assistant message after streaming.

    Yields tuples the API layer turns into SSE frames:

    - ``("delta", text)`` for each streamed token (many).
    - ``("done", {"sources", "conversation_id", "message_id"})`` once at the end.
    - ``("error", message)`` if anything fails; the generator never raises.
    """
    db = SessionLocal()
    try:
        cleaned = _sanitize_question(question)
        conversation = resolve_or_create_conversation(
            db, conversation_id, cleaned or question, owner_id
        )
        conv_id = conversation.id

        if not cleaned:
            _save_message(db, conv_id, "user", question or "")
            assistant_msg = _save_message(db, conv_id, "assistant", _NO_CONTEXT_ANSWER, sources=[])
            yield ("delta", _NO_CONTEXT_ANSWER)
            yield (
                "done",
                {
                    "sources": [],
                    "conversation_id": conv_id,
                    "message_id": assistant_msg.id,
                },
            )
            return

        # Greetings/small talk: short-circuit BEFORE retrieval so no sources
        # attach. Kept symmetric with the non-streaming query() path.
        if _is_smalltalk(cleaned):
            assistant_msg = _persist_smalltalk_turn(db, conv_id, cleaned)
            yield ("delta", _SMALLTALK_ANSWER)
            yield (
                "done",
                {
                    "sources": [],
                    "conversation_id": conv_id,
                    "message_id": assistant_msg.id,
                },
            )
            return

        _save_message(db, conv_id, "user", cleaned)

        # DB is the source of truth for history; prefer prior messages, but fall
        # back to a client-supplied history when present and DB has nothing.
        db_history = _load_history(db, conv_id)
        # Drop the just-saved user turn from history so it is not duplicated by
        # the prompt's explicit "Question:" line.
        effective_history: Sequence[Mapping[str, str]] | None
        if len(db_history) > 1:
            effective_history = db_history[:-1]
        else:
            effective_history = history

        system, user, sources = _retrieve_and_build_prompt(
            db, cleaned, top_k, effective_history, owner_id
        )

        chunks: list[str] = []
        for delta in _stream_answer_deltas(system, user):
            if not delta:
                continue
            chunks.append(delta)
            yield ("delta", delta)

        full_answer = "".join(chunks) or _NO_CONTEXT_ANSWER
        # The answer disclaims having found anything: drop sources so the "done"
        # frame and the persisted message don't surface unbacked chips.
        if sources and _is_no_answer(full_answer):
            sources = []
        assistant_msg = _save_message(db, conv_id, "assistant", full_answer, sources=sources)
        yield (
            "done",
            {
                "sources": sources,
                "conversation_id": conv_id,
                "message_id": assistant_msg.id,
            },
        )
    except Exception as exc:  # noqa: BLE001 - never raise out of the SSE generator.
        logger.error("stream_answer failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        yield ("error", "An error occurred while generating the answer.")
    finally:
        db.close()


def query(
    db: Session,
    question: str,
    top_k: int | None = None,
    history: Sequence[Mapping[str, str]] | None = None,
    conversation_id: int | None = None,
    *,
    owner_id: int,
) -> dict:
    """Answer ``question`` with RAG over indexed documents and persist the turn.

    Args:
        db: Database session used for retrieval/active-filtering and persistence.
        question: The user's natural-language question.
        top_k: Optional override for the number of chunks to retrieve.
        history: Optional prior conversation turns for follow-up support.
        conversation_id: Optional existing conversation to continue; a new one is
            created when omitted/null or not found.
        owner_id: The requesting user's id. Scopes retrieval (DEFAULT-DENY: own +
            admin-global docs only) and the conversation it is persisted to.

    Returns:
        ``{"answer", "sources", "conversation_id", "message_id"}`` where each
        source is ``{document, excerpt, document_id, page}`` (no score) and is one
        of the user's OWN documents. When nothing relevant is found, ``sources``
        is empty and the answer says so. The turn (user + assistant messages) is
        persisted to the resolved conversation.
    """
    cleaned = _sanitize_question(question)
    conversation = resolve_or_create_conversation(
        db, conversation_id, cleaned or question, owner_id
    )
    conv_id = conversation.id

    if not cleaned:
        _save_message(db, conv_id, "user", question or "")
        assistant_msg = _save_message(db, conv_id, "assistant", _NO_CONTEXT_ANSWER, sources=[])
        return {
            "answer": _NO_CONTEXT_ANSWER,
            "sources": [],
            "conversation_id": conv_id,
            "message_id": assistant_msg.id,
        }

    # Greetings/small talk: short-circuit BEFORE retrieval so no sources attach.
    if _is_smalltalk(cleaned):
        assistant_msg = _persist_smalltalk_turn(db, conv_id, cleaned)
        return {
            "answer": _SMALLTALK_ANSWER,
            "sources": [],
            "conversation_id": conv_id,
            "message_id": assistant_msg.id,
        }

    _save_message(db, conv_id, "user", cleaned)

    db_history = _load_history(db, conv_id)
    effective_history: Sequence[Mapping[str, str]] | None
    if len(db_history) > 1:
        effective_history = db_history[:-1]
    else:
        effective_history = history

    system, user, sources = _retrieve_and_build_prompt(
        db, cleaned, top_k, effective_history, owner_id
    )
    answer = _generate_answer(system, user)

    # The answer disclaims having found anything: drop sources so we don't
    # surface chips that didn't actually back the answer.
    if sources and _is_no_answer(answer):
        sources = []

    assistant_msg = _save_message(db, conv_id, "assistant", answer, sources=sources)

    return {
        "answer": answer,
        "sources": sources,
        "conversation_id": conv_id,
        "message_id": assistant_msg.id,
    }
