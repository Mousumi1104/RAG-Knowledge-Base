"""Semantic-aware text chunking for indexing.

Approach (documented):

- Token approximation: chunk sizes are measured in **characters**. This avoids a
  heavyweight tokenizer dependency; for English prose ~4 characters per token,
  so the default ``CHUNK_SIZE`` of 1000 chars is a conservative, token-safe
  window for typical embedding models.
- Semantic boundaries: text is first split into paragraphs (blank-line
  separated), then long paragraphs are split into sentences. Units are packed
  greedily up to ``chunk_size``; a character ``overlap`` tail from the previous
  chunk is prepended to the next to preserve continuity.
- Stable IDs: ``chunk_id`` is derived from the document id and the chunk's
  position (``"{doc_id}:{index}"``) so reindexing a document yields identical,
  stable ids. ``make_chunk_id`` exposes this for callers that need it.
"""

from __future__ import annotations

import re

from app.core.config import settings

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
# Split on sentence terminators followed by whitespace.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def make_chunk_id(doc_id: int | str, index: int) -> str:
    """Return a stable chunk id for a document position."""
    return f"{doc_id}:{index}"


def _split_units(text: str) -> list[str]:
    """Split text into semantic units (paragraphs, then sentences if needed)."""
    units: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        units.append(paragraph)
    return units


def _split_oversized(unit: str, chunk_size: int) -> list[str]:
    """Break a single unit larger than ``chunk_size`` into smaller pieces.

    Tries sentence boundaries first, then falls back to hard character slicing.
    """
    if len(unit) <= chunk_size:
        return [unit]

    pieces: list[str] = []
    buffer = ""
    for sentence in _SENTENCE_SPLIT.split(unit):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > chunk_size:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            # Hard-slice an over-long sentence.
            for start in range(0, len(sentence), chunk_size):
                pieces.append(sentence[start : start + chunk_size])
            continue
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if len(candidate) > chunk_size:
            pieces.append(buffer)
            buffer = sentence
        else:
            buffer = candidate
    if buffer:
        pieces.append(buffer)
    return pieces


def _normalize_overlap(chunk_size: int, overlap: int) -> int:
    """Clamp ``overlap`` into a sane range relative to ``chunk_size``."""
    if overlap < 0:
        return 0
    if overlap >= chunk_size:
        return chunk_size // 4
    return overlap


def _chunk_one(
    text: str,
    chunk_size: int,
    overlap: int,
    doc_id: int | str,
    start_index: int,
    page: int | None,
    chunks: list[dict],
) -> int:
    """Pack ``text`` into chunks, appending to ``chunks``; return the next index.

    Positions/chunk_ids continue from ``start_index`` so callers chunking many
    pages keep a single, document-global, gap-free index across pages. Each
    appended chunk is tagged with ``page`` (``None`` for raw-text callers).
    """
    text = (text or "").strip()
    if not text:
        return start_index

    # Expand semantic units, splitting any that exceed the chunk size.
    units: list[str] = []
    for unit in _split_units(text):
        units.extend(_split_oversized(unit, chunk_size))

    buffer = ""
    index = start_index

    def flush() -> None:
        nonlocal buffer, index
        if not buffer.strip():
            return
        chunks.append(
            {
                "chunk_id": make_chunk_id(doc_id, index),
                "content": buffer.strip(),
                "position": index,
                "page": page,
            }
        )
        index += 1
        # Seed the next buffer with the overlap tail for continuity.
        buffer = buffer[-overlap:] if overlap else ""

    for unit in units:
        candidate = f"{buffer}\n\n{unit}".strip() if buffer else unit
        if len(candidate) > chunk_size and buffer.strip():
            flush()
            candidate = f"{buffer}\n\n{unit}".strip() if buffer else unit
        buffer = candidate

    flush()
    return index


def chunk_text(
    text: str,
    chunk_size: int = settings.CHUNK_SIZE,
    overlap: int = settings.CHUNK_OVERLAP,
    doc_id: int | str = "doc",
) -> list[dict]:
    """Split ``text`` into overlapping, semantically-aware chunks.

    Args:
        text: Cleaned source text.
        chunk_size: Maximum characters (~token-safe) per chunk.
        overlap: Number of trailing characters carried into the next chunk.
        doc_id: Document id used to build stable ``chunk_id`` values.

    Returns:
        A list of ``{"chunk_id": str, "content": str, "position": int, "page": None}``
        dicts. ``page`` is always ``None`` for raw-text callers (no page context).
    """
    overlap = _normalize_overlap(chunk_size, overlap)
    chunks: list[dict] = []
    _chunk_one(text, chunk_size, overlap, doc_id, 0, None, chunks)
    return chunks


def chunk_pages(
    pages: list[tuple[int, str]],
    chunk_size: int = settings.CHUNK_SIZE,
    overlap: int = settings.CHUNK_OVERLAP,
    doc_id: int | str = "doc",
) -> list[dict]:
    """Chunk per page, tagging each chunk with its 1-based source ``page``.

    Each page is chunked independently (chunks never span a page boundary), but
    positions/``chunk_id`` values run as a single document-global, gap-free index
    across all pages — so ``chunk_id`` values stay unique and stable.

    Args:
        pages: ``(page_number, cleaned_text)`` tuples (e.g. from
            :func:`app.services.extraction.extract_pages`), in page order.
        chunk_size: Maximum characters (~token-safe) per chunk.
        overlap: Number of trailing characters carried into the next chunk.
        doc_id: Document id used to build stable ``chunk_id`` values.

    Returns:
        A list of ``{"chunk_id": str, "content": str, "position": int, "page": int}``
        dicts, ordered by page then position.
    """
    overlap = _normalize_overlap(chunk_size, overlap)
    chunks: list[dict] = []
    next_index = 0
    for page, text in pages:
        next_index = _chunk_one(
            text, chunk_size, overlap, doc_id, next_index, page, chunks
        )
    return chunks
