"""Text extraction and cleaning for supported document types.

Extraction is dependency-light: ``pypdf`` for PDF, ``python-docx`` for DOCX, and
a plain UTF-8 read (errors ignored) for TXT/MD. Cleaning normalizes whitespace
and strips control characters so downstream chunking/embedding sees clean text.
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager

from app.utils.errors import UnsupportedFileTypeError
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Timeout (seconds) for downloading remotely-stored documents before extraction.
_DOWNLOAD_TIMEOUT = 30.0


def _is_remote(file_path: str) -> bool:
    """True when ``file_path`` is an http(s) URL rather than a local path."""
    return file_path.startswith("http://") or file_path.startswith("https://")


@contextmanager
def _local_path(file_path: str, document_type: str) -> Iterator[str]:
    """Yield a usable local path for ``file_path``.

    Local paths are yielded unchanged. Remote http(s) URLs (e.g. Cloudinary)
    are downloaded into a temporary file (extension preserved so library
    sniffing behaves) which is removed on exit.
    """
    if not _is_remote(file_path):
        yield file_path
        return

    import httpx

    with httpx.Client(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT) as client:
        response = client.get(file_path)
        response.raise_for_status()
        data = response.content

    suffix = f".{(document_type or '').lower().lstrip('.')}" if document_type else ""
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        yield tmp_path
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

# Control characters except common whitespace (tab, newline, carriage return).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Rupee-sign recovery: the Indian rupee glyph ₹ (U+20B9) is not present in
# ``WinAnsiEncoding``, so pypdf cannot decode it from PDFs whose Helvetica font
# declares that encoding (the common case for ₹ amounts). It surfaces instead as
# the filled-square replacement glyph ■ (U+25A0) sitting immediately before the
# numeric amount (e.g. "limit: ■5000 per night"). We remap a ■ that directly
# precedes a digit (optionally across a single space) back to ₹ so currency
# amounts survive extraction and become searchable/answerable. This is narrowly
# targeted at the corruption pattern (box-then-digit); a standalone ■ used as a
# real bullet/checkbox glyph is left untouched.
_RUPEE_BOX_RE = re.compile(r"■(\s?)(\d)")


def _extract_pdf_pages(file_path: str) -> list[str]:
    """Return per-page raw text for a PDF (one string per page, in order)."""
    from pypdf import PdfReader

    reader = PdfReader(file_path)
    pages: list[str] = []
    for page in reader.pages:
        # ``layout`` mode preserves the visual line/word grouping, so words stay
        # space-joined within a line. The default mode positions glyphs and often
        # emits one word per line for table/justified layouts, which downstream
        # cleaning turns into blank-line-separated words (garbled, unsearchable).
        # Fall back to the default mode per-page if layout extraction fails.
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except Exception as exc:  # noqa: BLE001 - try default mode, then skip page
            logger.warning(
                "Layout PDF extraction failed for a page of %s (%s); trying default mode",
                file_path,
                exc,
            )
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc2:  # noqa: BLE001 - skip unreadable page, keep going
                logger.warning("Failed to extract a PDF page from %s: %s", file_path, exc2)
                # Keep page numbering aligned with the source even on failure.
                pages.append("")
    return pages


def _extract_docx(file_path: str) -> str:
    import docx

    document = docx.Document(file_path)
    return "\n".join(p.text for p in document.paragraphs)


def _extract_text_file(file_path: str) -> str:
    with open(file_path, encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def extract_pages(file_path: str, document_type: str) -> list[tuple[int, str]]:
    """Extract cleaned text per source page from ``file_path``.

    For PDFs, returns one ``(page_number, cleaned_text)`` tuple per page with
    1-based page numbers (page 1 first). For DOCX/TXT/MD — which have no inherent
    page model here — returns a single ``[(1, cleaned_full_text)]`` entry.

    Args:
        file_path: Absolute or relative path to the stored file.
        document_type: One of ``pdf`` | ``docx`` | ``txt`` | ``md``.

    Returns:
        A list of ``(page_number, cleaned_text)`` tuples, 1-based, in page order.

    Raises:
        UnsupportedFileTypeError: if ``document_type`` is not supported.
    """
    doc_type = (document_type or "").lower().lstrip(".")

    if doc_type not in ("pdf", "docx", "txt", "md"):
        raise UnsupportedFileTypeError(
            f"Cannot extract text from unsupported type '{document_type}'"
        )

    # Resolve remote (Cloudinary) URLs to a temp file; local paths pass through.
    with _local_path(file_path, doc_type) as local:
        if doc_type == "pdf":
            return [
                (page_number, clean_text(raw))
                for page_number, raw in enumerate(_extract_pdf_pages(local), start=1)
            ]
        if doc_type == "docx":
            return [(1, clean_text(_extract_docx(local)))]
        # txt / md
        return [(1, clean_text(_extract_text_file(local)))]


def extract_text(file_path: str, document_type: str) -> str:
    """Extract cleaned plain text from ``file_path`` based on ``document_type``.

    Thin wrapper over :func:`extract_pages` that joins per-page text with blank
    lines, preserving the original single-string contract for existing callers.

    Args:
        file_path: Absolute or relative path to the stored file.
        document_type: One of ``pdf`` | ``docx`` | ``txt`` | ``md``.

    Returns:
        Cleaned plain text extracted from the document.

    Raises:
        UnsupportedFileTypeError: if ``document_type`` is not supported.
    """
    pages = extract_pages(file_path, document_type)
    return "\n\n".join(text for _page, text in pages if text).strip()


def clean_text(text: str) -> str:
    """Normalize whitespace and strip control characters from ``text``.

    - Normalizes Unicode to NFKC.
    - Removes control characters (keeping ``\\t``, ``\\n``, ``\\r``).
    - Collapses runs of spaces/tabs and trims trailing space on each line.
    - Collapses 3+ blank lines into a single blank line (paragraph boundary).
    """
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    # Recover ₹ amounts that pypdf decoded as the ■ box glyph (see _RUPEE_BOX_RE).
    text = _RUPEE_BOX_RE.sub(r"₹\1\2", text)

    # Collapse horizontal whitespace and trim each line.
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)

    # Collapse excessive blank lines down to a single paragraph separator.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
