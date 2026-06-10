"""Prompt construction for the RAG chat pipeline.

The single entry point :func:`build_rag_prompt` turns a user question plus the
retrieved context chunks (and optional prior conversation) into a ``(system,
user)`` prompt pair. The instructions are deliberately strict: the model must
answer **only** from the supplied context, must refuse / disclaim when the
answer is not present, and must cite the source document names. Each excerpt is
numbered and labelled with its document name so the model can reference it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# Grounding instructions shared across providers. Strict for document questions,
# but conversational for greetings/small talk so "Hello" isn't met with a
# "couldn't find it in the knowledge base" disclaimer.
_SYSTEM_PROMPT = (
    "You are a helpful, friendly knowledge-base assistant. How to respond:\n"
    "- If the user's message is a greeting, thanks, or casual small talk (not a "
    "question about the documents), reply naturally and briefly, and invite them "
    "to ask about the knowledge base. Do NOT mention missing context or add a "
    "source citation for these.\n"
    "- For questions about the documents, answer using ONLY the information in "
    "the provided context excerpts below. Do not use outside knowledge and do "
    "not invent or assume any facts. If the answer cannot be found in the "
    "context, say clearly that you could not find the information in the "
    "knowledge base.\n"
    "- You may and SHOULD REASON over the context: combine and connect related "
    "statements across the excerpts to arrive at the answer, even when no single "
    "sentence restates the question verbatim. If the context defines a rule for a "
    "category and also places a specific item within (or excludes it from) that "
    "category, apply the rule to that item and state the resulting answer "
    "directly and confidently. Treat such a logically entailed answer as found in "
    "the context, NOT as missing. Do not refuse or hedge merely because the "
    "wording differs from the question or because the answer requires connecting "
    "two facts. Only say you could not find the information when the facts needed "
    "to make that connection are genuinely absent from the context. When the "
    "answer follows from such a connection, give the answer first, then briefly "
    "note the link you relied on. For example, if the context states that "
    "'overdue items are not intended to be returned as on-time items' and "
    "separately that 'on-time items must be returned within 7 days', then to "
    "'how long do I have to return an item?' you should answer 'within 7 days' "
    "(because the item falls under the on-time rule), rather than saying the "
    "return window is not specified.\n"
    "- Be concise and accurate.\n"
    "- When you use the context, cite the source document name(s), e.g. "
    "(Source: HR_Policy.pdf)."
)

_NO_CONTEXT_NOTE = (
    "No context excerpts were retrieved for this message. If it is a question "
    "about the documents, tell the user you could not find relevant information "
    "in the knowledge base. If it is a greeting or casual remark, simply respond "
    "naturally and briefly and invite them to ask a question about the documents."
)


def _format_history(history: Sequence[Mapping[str, str]] | None) -> str:
    """Render optional prior conversation turns as a readable transcript."""
    if not history:
        return ""
    lines: list[str] = []
    for turn in history:
        role = str(turn.get("role", "user")).strip() or "user"
        content = str(turn.get("content", "")).strip()
        if not content:
            continue
        speaker = "User" if role.lower() == "user" else "Assistant"
        lines.append(f"{speaker}: {content}")
    if not lines:
        return ""
    return "Prior conversation:\n" + "\n".join(lines) + "\n\n"


def _format_contexts(contexts: Sequence[Mapping[str, object]]) -> str:
    """Render retrieved contexts as a numbered, document-labelled block."""
    if not contexts:
        return _NO_CONTEXT_NOTE
    blocks: list[str] = []
    for index, ctx in enumerate(contexts, start=1):
        document_name = str(ctx.get("document_name") or "unknown document")
        excerpt = str(ctx.get("excerpt") or "").strip()
        blocks.append(f"[{index}] Document: {document_name}\n{excerpt}")
    return "Context excerpts:\n" + "\n\n".join(blocks)


def build_rag_prompt(
    query: str,
    contexts: Sequence[Mapping[str, object]],
    history: Sequence[Mapping[str, str]] | None = None,
) -> tuple[str, str]:
    """Build the ``(system, user)`` prompt pair for a RAG generation call.

    Args:
        query: The user's natural-language question.
        contexts: Retrieved chunks, each a mapping with at least
            ``document_name`` and ``excerpt`` keys (the shape returned by
            ``search_service.search``).
        history: Optional prior conversation as a sequence of
            ``{"role", "content"}`` mappings, for follow-up support.

    Returns:
        A ``(system_prompt, user_prompt)`` tuple. The system prompt carries the
        grounding rules; the user prompt carries the context, optional history
        and the question.
    """
    history_block = _format_history(history)
    context_block = _format_contexts(contexts)
    user_prompt = (
        f"{history_block}{context_block}\n\n"
        f"Question: {(query or '').strip()}\n\n"
        "Answer (grounded only in the context above, with source citations):"
    )
    return _SYSTEM_PROMPT, user_prompt
