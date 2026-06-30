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


def _linearize_table(table: list[list[str | None]]) -> list[str]:
    """Turn one extracted table grid into self-contained, atomic text lines.

    A 2-D table (e.g. a schedule matrix of dates x workstations) loses all
    meaning when flattened to layout text: column headers end up far from the
    cells they label, so a value can no longer be tied to its column. We restore
    that association by emitting, for a grid that has group/banner labels, ONE
    short line per data cell that repeats the row's key columns, e.g.::

        1-Jul-26 (Wed, Working Day) - WS#9 (Room 11): Mousumi Sengupta

    Per-cell (not per-row) lines are deliberate: the chunker hard-slices any unit
    longer than the chunk size, so a long per-row line would be cut mid-row and
    re-detach a seat from its date. Each atomic line stays well under the chunk
    size and carries date+seat+room+name together, so retrieval is reliable.
    Tables without banner/group rows fall back to one "Header: value | ..." line
    per row (typically short).

    Returns a list of lines; the caller joins them with blank lines so the
    chunker treats each as its own semantic unit and never splits one.
    """
    rows = [
        [(c or "").strip() for c in row]
        for row in table
        if any((c or "").strip() for c in row)
    ]
    if len(rows) < 2:
        return []
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]

    # Header = the row with the most non-empty cells (earliest wins on a tie).
    counts = [sum(1 for c in r if c) for r in rows]
    h = max(range(len(rows)), key=lambda i: counts[i])
    header = rows[h]

    # Banner rows above the header become forward-filled per-column group labels
    # (so "Room 16"/"Room 11" attach to the right workstation columns); a banner
    # row with a single value is treated as a table caption instead.
    group = [""] * ncols
    captions: list[str] = []
    for r in rows[:h]:
        non_empty = [c for c in r if c]
        if len(non_empty) == 1:
            captions.append(non_empty[0])
            continue
        if len(non_empty) >= 2:
            last = ""
            for i in range(ncols):
                if r[i]:
                    last = r[i]
                if last:
                    group[i] = (group[i] + " " + last).strip()

    def label_for(i: int) -> str:
        return header[i] if i < len(header) and header[i] else f"Col{i + 1}"

    # Key columns = the leading run that shares column 0's group label; the first
    # column whose group differs marks the start of the value columns. When there
    # are no group labels at all, every column is "key" and we fall back to a
    # compact per-row line.
    key_end = ncols
    for i in range(ncols):
        if group[i] != group[0]:
            key_end = i
            break

    lines: list[str] = []
    # value -> ordered-unique row keys it appears against (for aggregate summaries)
    occurrences: dict[str, list[str]] = {}
    for r in rows[h + 1:]:
        if not any(r):
            continue
        key_vals = [r[i] for i in range(key_end) if r[i]]
        if key_vals:
            prefix = key_vals[0]
            if len(key_vals) > 1:
                prefix += " (" + ", ".join(key_vals[1:]) + ")"
        else:
            prefix = ""

        value_cols = [i for i in range(key_end, ncols) if r[i]]
        if value_cols and prefix:
            row_key = key_vals[0]
            for i in value_cols:
                grp = f" ({group[i]})" if group[i] else ""
                lines.append(f"{prefix} - {label_for(i)}{grp}: {r[i]}")
                seen = occurrences.setdefault(r[i], [])
                if row_key not in seen:
                    seen.append(row_key)
        else:
            # No clean key/value split (e.g. a plain table) -> compact per-row line.
            parts = [
                f"{label_for(i)}: {r[i]}" for i in range(ncols) if r[i]
            ]
            if parts:
                lines.append(" | ".join(parts))

    # Aggregate summary lines: a value that appears against many row keys is
    # spread across many chunks, so top-k retrieval can never gather them all to
    # answer a "how many ..." count. Emit one self-contained summary per such
    # value (count + the full row-key list) so a single chunk answers it.
    if occurrences:
        key_label = (header[0] if header and header[0] else "").lower()
        sample_key = next(iter(occurrences.values()))[0] if occurrences else ""
        date_like = bool(re.match(r"\d+[-/][A-Za-z0-9]+[-/]\d", sample_key))
        if "date" in key_label or "day" in key_label or date_like:
            unit = "days"
        elif key_label:
            unit = key_label if key_label.endswith("s") else key_label + "s"
        else:
            unit = "entries"
        caption = "; ".join(captions)

        def _key_sort(k: str) -> tuple[int, str]:
            m = re.match(r"\s*(\d+)", k)
            return (int(m.group(1)) if m else 1_000_000, k)

        summary_lines: list[str] = []
        for value, keys in occurrences.items():
            if len(keys) < 2:
                continue
            ordered = sorted(keys, key=_key_sort)
            line = f"{value} - total {len(ordered)} {unit}: {', '.join(ordered)}"
            if caption:
                line += f" ({caption})"
            summary_lines.append(line)
        lines.extend(summary_lines)

    if captions and lines:
        lines.insert(0, "Table: " + "; ".join(captions))
    return lines


def _extract_pdf_tables_text(file_path: str) -> dict[int, str]:
    """Return a page_number -> structured-table-text map for a PDF.

    Best-effort and additive: any failure (or a PDF with no detectable tables)
    yields no structured text for that page and the caller keeps the normal
    layout extraction. Never raises — indexing must not break on a tricky PDF.
    """
    try:
        import pdfplumber
    except Exception as exc:  # noqa: BLE001 - dependency missing/broken; skip tables
        logger.warning("pdfplumber unavailable, skipping table extraction: %s", exc)
        return {}

    structured: dict[int, str] = {}
    try:
        with pdfplumber.open(file_path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                try:
                    tables = page.extract_tables() or []
                except Exception as exc:  # noqa: BLE001 - skip this page's tables
                    logger.warning("Table extraction failed on page %d: %s", page_number, exc)
                    continue
                page_lines: list[str] = []
                for table in tables:
                    page_lines.extend(_linearize_table(table))
                if page_lines:
                    # Blank-line separated so the chunker treats each atomic line
                    # as its own semantic unit and never slices one mid-line.
                    structured[page_number] = "\n\n".join(page_lines)
    except Exception as exc:  # noqa: BLE001 - whole-file fallback, keep layout text
        logger.warning("pdfplumber could not open %s for tables: %s", file_path, exc)
        return {}
    return structured


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
            # Table-aware extraction (additive): append per-row structured text so
            # each cell stays tied to its column header (e.g. a workstation number
            # to the person sitting at it). Falls back silently to layout-only text
            # for non-table PDFs or if pdfplumber fails.
            tables_by_page = _extract_pdf_tables_text(local)
            pages: list[tuple[int, str]] = []
            for page_number, raw in enumerate(_extract_pdf_pages(local), start=1):
                text = clean_text(raw)
                structured = tables_by_page.get(page_number)
                if structured:
                    structured = clean_text(structured)
                    text = (
                        f"{text}\n\nStructured table data:\n{structured}"
                        if text
                        else f"Structured table data:\n{structured}"
                    )
                pages.append((page_number, text))
            return pages
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
