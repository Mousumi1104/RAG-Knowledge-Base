"""Cloudinary-backed remote storage for uploaded document files.

The Cloudinary SDK is configured lazily from :data:`app.core.config.settings`
and only when all three credentials are present (``cloudinary_enabled``). This
keeps the module import-safe and the local-disk fallback fully intact when
Cloudinary is not configured.

Files are uploaded as ``raw`` resources (Cloudinary's mode for non-image/video
assets such as PDFs, DOCX, TXT, MD) under a single folder so the knowledge base
stays tidy.
"""

from __future__ import annotations

import io
from urllib.parse import urlparse

from app.core.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_FOLDER = "rag-knowledge-base"
_configured = False


def is_remote(path: str | None) -> bool:
    """Return True when ``path`` is an http(s) URL (a remote/Cloudinary file)."""
    if not path:
        return False
    return path.startswith("http://") or path.startswith("https://")


def _configure() -> None:
    """Configure the Cloudinary SDK once, from settings."""
    global _configured
    if _configured:
        return
    import cloudinary

    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )
    _configured = True


def upload_bytes(data: bytes, filename: str) -> str:
    """Upload ``data`` to Cloudinary and return its ``secure_url``.

    Args:
        data: Raw file bytes.
        filename: Original filename; used to derive a readable public id.

    Returns:
        The HTTPS ``secure_url`` of the stored asset.
    """
    _configure()
    import cloudinary.uploader

    result = cloudinary.uploader.upload(
        io.BytesIO(data),
        resource_type="raw",
        folder=_FOLDER,
        use_filename=True,
        unique_filename=True,
    )
    return result["secure_url"]


def _public_id_from_url(url: str) -> str | None:
    """Best-effort parse of a Cloudinary public_id (incl. folder) from a URL.

    A raw delivery URL looks like::

        https://res.cloudinary.com/<cloud>/raw/upload/v123/rag-knowledge-base/<name>.pdf

    The public_id is everything after the version segment (``v123/``), i.e.
    ``rag-knowledge-base/<name>.pdf`` for raw resources (extension included).
    """
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    parts = [p for p in path.split("/") if p]
    if "upload" not in parts:
        return None
    idx = parts.index("upload") + 1
    # Skip an optional version segment like ``v1234567890``.
    if idx < len(parts) and parts[idx].startswith("v") and parts[idx][1:].isdigit():
        idx += 1
    remainder = parts[idx:]
    if not remainder:
        return None
    return "/".join(remainder)


def delete_url(url: str) -> None:
    """Best-effort deletion of a Cloudinary asset given its delivery URL.

    Failures are logged, never raised, so caller cleanup paths stay resilient.
    """
    if not is_remote(url):
        return
    public_id = _public_id_from_url(url)
    if not public_id:
        logger.warning("Could not parse Cloudinary public_id from URL: %s", url)
        return
    try:
        _configure()
        import cloudinary.uploader

        cloudinary.uploader.destroy(public_id, resource_type="raw")
    except Exception as exc:  # noqa: BLE001 - deletion is best-effort cleanup
        logger.warning("Cloudinary delete failed for %s: %s", url, exc)
