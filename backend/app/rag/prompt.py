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
from datetime import datetime, timedelta, timezone

# India Standard Time is a fixed UTC+05:30 offset with no DST. We use a fixed
# offset rather than ``zoneinfo`` because the production runtime image
# (``python:3.12-slim``) ships no system tzdata, so ``ZoneInfo("Asia/Kolkata")``
# would raise ``ZoneInfoNotFoundError``.
IST = timezone(timedelta(hours=5, minutes=30))


def _current_ist_line(now: datetime | None = None) -> str:
    """Build the authoritative real-time clock bullet for the system prompt.

    Args:
        now: An optional timezone-aware moment to render (defaults to the
            current time in IST). Injectable so tests stay deterministic.

    Returns:
        A single instructional bullet that states the present date and time in
        India Standard Time and tells the model to treat it as the authoritative
        present moment, exempt from the context-only grounding rule.
    """
    if now is None:
        now = datetime.now(IST)
    # Avoid the non-portable ``%-d``/``%-I``: derive the no-leading-zero day and
    # hour manually so the line reads naturally (e.g. "30 June 2026, 1:05 PM").
    day = str(int(now.strftime("%d")))
    hour = str(int(now.strftime("%I")))
    value = now.strftime(f"%A, {day} %B %Y, {hour}:%M %p IST")
    return (
        "- Current date and time in India (IST, UTC+05:30): "
        f"{value}. When the user asks about the current date, time, day of the "
        "week, or uses relative words like 'today', 'now', 'this week', or "
        "'current', treat this as the present moment. This real-time clock "
        "information is authoritative and is exempt from the context-only "
        "grounding rule above."
    )


def _current_employee_line(name: str | None, emp_id: str | None) -> str:
    """Build the authoritative "Current employee" system-prompt bullet.

    Returns "" when neither name nor id is supplied (e.g. the regular user/admin
    chat path), so the system prompt is byte-identical to before in that case.
    Mirrors :func:`_current_ist_line`: the identity is stated as ground truth and
    exempt from the context-only grounding rule. See EZ chat auth spec §3.
    """
    name = (name or "").strip()
    emp_id = (emp_id or "").strip()
    if not name and not emp_id:
        return ""
    who = name or "the employee"
    id_part = f" (employee ID {emp_id})" if emp_id else ""
    return (
        f"\n- Current employee: you are assisting {who}{id_part}. When they say "
        "'I', 'me' or 'my', this is who they mean; address them by name when it "
        "reads naturally. This identity is authoritative and exempt from the "
        "context-only grounding rule above."
    )

# Grounding instructions shared across providers. Strict for document questions,
# but conversational for greetings/small talk so "Hello" isn't met with a
# "couldn't find it in the knowledge base" disclaimer.
_SYSTEM_PROMPT = (
    "You are a helpful, friendly, knowledgeable human expert who assists people "
    "using an internal knowledge base. How to respond:\n"
    "- Speak in a natural, warm, human voice, the way a knowledgeable colleague "
    "would. NEVER begin a reply with robotic meta-prefaces such as 'Based on the "
    "document(s)', 'Based on the provided context', 'Based on the provided "
    "information', 'According to the document(s)', 'According to the text', 'As "
    "per the context', or 'The provided text states'. Just answer the question "
    "directly and conversationally.\n"
    "- Never refer to yourself as an AI, a model, a language model, a bot, an "
    "assistant program, or software, and never add 'as an AI' style disclaimers. "
    "You are a helpful human expert; simply help.\n"
    "- If the user's message is a greeting, thanks, or casual small talk (not a "
    "question about the documents), reply naturally and briefly, and invite them "
    "to ask about the knowledge base. Do NOT mention missing context or add a "
    "source citation for these.\n"
    "- For questions about the documents, answer using ONLY the information in "
    "the provided context excerpts below. Do not use outside knowledge and do "
    "not invent or assume any facts. If the answer cannot be found in the "
    "context, say so naturally and humanly, for example: \"I couldn't find "
    "anything about that in the knowledge base.\"\n"
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
    "- ALWAYS answer in the SAME language and script as the user's question, "
    "regardless of the language of the context excerpts. If the question is in "
    "English, answer in English; if it is in Hindi (Devanagari), answer in Hindi "
    "(Devanagari); if it is in Bengali, answer in Bengali. The source context may "
    "be in English even when the question is not — translate the grounded answer "
    "into the question's language. You may keep proper nouns, acronyms and "
    "numeric/currency values (e.g. Ananya Sharma, MFA, ₹5000) in their original "
    "form. This language rule never relaxes the grounding rules above: still "
    "answer only from the context, and give the not-found message in the user's "
    "language when the answer is absent.\n"
    "- When you use the context, you may cite the source document name(s), but "
    "do it naturally and place the citation at the END of your answer as a brief "
    "note, e.g. a final '(Source: HR_Policy.pdf)'. Never open a reply with the "
    "citation or a 'based on the source' style preface.\n"
    "- Formatting: write answers in Markdown. Use bullet or numbered lists for "
    "enumerations, Markdown tables for tabular/comparative data, `**bold**` for "
    "emphasis and fenced code blocks for code or commands. Render all "
    "mathematics, formulas, scientific notation and chemical formulas with LaTeX: "
    "use `$...$` for inline math and `$$...$$` for display math, including "
    "subscripts and superscripts (e.g. water as $H_2O$, a cube as $x^3$, and one "
    "nanometre as $10^{-9}\\,\\text{m}$). Do not wrap the whole answer in a code "
    "block, and keep formatting purposeful rather than decorative."
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
    employee_name: str | None = None,
    employee_id: str | None = None,
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
    system_prompt = (
        _SYSTEM_PROMPT
        + "\n"
        + _current_ist_line()
        + _current_employee_line(employee_name, employee_id)
    )
    return system_prompt, user_prompt
