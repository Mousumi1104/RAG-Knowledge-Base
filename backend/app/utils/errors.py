"""Domain exceptions and FastAPI exception handlers.

A small hierarchy of domain exceptions is mapped to consistent JSON error
responses of the shape ``{"detail": ...}``. Internal details are logged
server-side but not leaked to clients.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.utils.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for domain errors mapped to HTTP responses."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    detail: str = "Application error"

    def __init__(self, detail: str | None = None) -> None:
        if detail is not None:
            self.detail = detail
        super().__init__(self.detail)


class NotFoundError(AppError):
    """Requested resource does not exist (or is soft-deleted)."""

    status_code = status.HTTP_404_NOT_FOUND
    detail = "Resource not found"


class ValidationError(AppError):
    """Request payload failed domain-level validation."""

    # 422 Unprocessable Content/Entity (numeric literal for version-agnosticism).
    status_code = 422
    detail = "Validation error"


class UnsupportedFileTypeError(AppError):
    """Uploaded file type is not permitted."""

    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    detail = "Unsupported file type"


class ProviderError(AppError):
    """An embedding/LLM/vector-store provider failed."""

    status_code = status.HTTP_502_BAD_GATEWAY
    detail = "Provider error"


class RateLimitError(AppError):
    """An upstream provider rate limit was exhausted."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    detail = "Rate limit exceeded"


def _json_error(status_code: int, detail: object) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers that produce consistent ``{"detail": ...}`` responses."""

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        logger.info("AppError: %s (%s)", exc.detail, exc.status_code)
        return _json_error(exc.status_code, exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _json_error(exc.status_code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _json_error(422, exc.errors())

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return _json_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal server error")
