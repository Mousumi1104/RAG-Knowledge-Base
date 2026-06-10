"""Central structured logging configuration.

Log level is read from the ``LOG_LEVEL`` environment variable (default ``INFO``).
No secrets are ever logged.
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging() -> None:
    """Configure root logging once for the whole application.

    Idempotent: repeated calls are no-ops so app factories / tests can call it freely.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers on reload.
    root.handlers.clear()
    root.addHandler(handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, ensuring logging has been configured."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
