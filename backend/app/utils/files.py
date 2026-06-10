"""File validation and safe-filename utilities.

Enforces the configured allowed extensions and maximum upload size, and
sanitizes filenames to strip path components and prevent directory traversal
before any file is persisted.
"""

from __future__ import annotations

import os
import re

from app.core.config import settings
from app.utils.errors import UnsupportedFileTypeError, ValidationError

# 413 Payload Too Large — surfaced for oversized uploads.
_HTTP_PAYLOAD_TOO_LARGE = 413

# Whitelist of characters permitted in a stored filename.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class PayloadTooLargeError(ValidationError):
    """Uploaded file exceeds the configured maximum size."""

    status_code = _HTTP_PAYLOAD_TOO_LARGE
    detail = "Uploaded file is too large"


def detect_document_type(filename: str) -> str:
    """Return the lowercased file extension (without the dot).

    Raises :class:`ValidationError` if the filename has no extension.
    """
    _, ext = os.path.splitext(filename or "")
    ext = ext.lower().lstrip(".")
    if not ext:
        raise ValidationError("Filename has no extension")
    return ext


def sanitize_filename(name: str) -> str:
    """Return a safe basename: strip path components and unsafe characters.

    - Uses ``os.path.basename`` to drop any directory portion (traversal safe).
    - Replaces any character outside ``[A-Za-z0-9._-]`` with ``_``.
    - Collapses leading dots so the result can never be a dotfile / ``..``.
    """
    base = os.path.basename(name or "")
    # Normalize backslashes (Windows-style paths) before basename fallback.
    base = base.replace("\\", "/").split("/")[-1]
    cleaned = _SAFE_CHARS.sub("_", base).strip("._")
    if not cleaned:
        cleaned = "upload"
    return cleaned


def validate_upload(filename: str, size_bytes: int) -> str:
    """Validate an upload's extension and size; return the detected type.

    Raises:
        UnsupportedFileTypeError: extension not in ``settings.ALLOWED_EXTENSIONS``.
        PayloadTooLargeError: file exceeds ``settings.MAX_UPLOAD_MB`` (only when a
            positive limit is configured).
        ValidationError: filename has no extension or file is empty.
    """
    doc_type = detect_document_type(filename)

    allowed = {ext.lower().lstrip(".") for ext in settings.ALLOWED_EXTENSIONS}
    if doc_type not in allowed:
        raise UnsupportedFileTypeError(
            f"Unsupported file type '.{doc_type}'. " f"Allowed: {', '.join(sorted(allowed))}"
        )

    if size_bytes <= 0:
        raise ValidationError("Uploaded file is empty")

    # ``MAX_UPLOAD_MB <= 0`` (or None) means *unlimited* — skip the size guard.
    max_mb = settings.MAX_UPLOAD_MB
    if max_mb and max_mb > 0:
        max_bytes = max_mb * 1024 * 1024
        if size_bytes > max_bytes:
            raise PayloadTooLargeError(
                f"File size {size_bytes} bytes exceeds the " f"{max_mb} MB limit"
            )

    return doc_type
