"""Admin API routes.

Thin router: validate input, delegate to ``admin_service``, shape JSON. Exposes
operational diagnostics (stats, indexing status, health) and a reindex trigger.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.security import get_current_admin, require_admin
from app.database.session import get_db
from app.models.user import User
from app.schemas.admin import (
    HealthResponse,
    IndexingStatusResponse,
    ReindexRequest,
    ReindexResponse,
    StatsResponse,
)
from app.schemas.user import UserOut
from app.services import admin_service, user_service
from app.utils.logging import get_logger

logger = get_logger(__name__)

# All admin routes require a valid admin JWT.
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_admin)],
)


@router.get("/stats", response_model=StatsResponse)
def get_stats(db: Session = Depends(get_db)) -> StatsResponse:
    """Aggregate stats over ACTIVE documents plus vector-store status."""
    logger.info("GET /admin/stats")
    try:
        return StatsResponse(**admin_service.get_stats(db))
    except Exception as exc:  # noqa: BLE001 - surface as a 500 with context
        logger.exception("admin.get_stats failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to compute stats") from exc


@router.get("/indexing-status", response_model=IndexingStatusResponse)
def get_indexing_status(db: Session = Depends(get_db)) -> IndexingStatusResponse:
    """Per-document indexing status breakdown for ACTIVE documents."""
    logger.info("GET /admin/indexing-status")
    try:
        return IndexingStatusResponse(**admin_service.get_indexing_status_overview(db))
    except Exception as exc:  # noqa: BLE001
        logger.exception("admin.get_indexing_status failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load indexing status") from exc


@router.post("/reindex", response_model=ReindexResponse)
def reindex(
    payload: ReindexRequest | None = Body(default=None),
    document_id: int | None = Query(
        default=None, description="Reindex a single document; omit to reindex all."
    ),
    db: Session = Depends(get_db),
) -> ReindexResponse:
    """Reindex one document (body or query ``document_id``) or all ACTIVE docs."""
    target = document_id
    if target is None and payload is not None:
        target = payload.document_id
    logger.info("POST /admin/reindex document_id=%s", target)
    try:
        summary = admin_service.trigger_reindex(db, target)
    except Exception as exc:  # noqa: BLE001
        logger.exception("admin.trigger_reindex failed: %s", exc)
        raise HTTPException(status_code=500, detail="Reindex failed") from exc
    return ReindexResponse(**summary)


@router.get("/health", response_model=HealthResponse)
def get_health(db: Session = Depends(get_db)) -> HealthResponse:
    """Check database connectivity and vector-store health."""
    logger.info("GET /admin/health")
    return HealthResponse(**admin_service.health(db))


# --- User management ---------------------------------------------------------
@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    """List all non-deleted users (safe ``UserOut`` — never includes hashes)."""
    logger.info("GET /admin/users")
    return user_service.list_users(db)


@router.post("/users/{user_id}/enable", response_model=UserOut)
def enable_user(user_id: int, db: Session = Depends(get_db)) -> User:
    """Re-enable a disabled user (``is_active = True``)."""
    logger.info("POST /admin/users/%s/enable", user_id)
    return user_service.set_active(db, user_id, True)


@router.post("/users/{user_id}/disable", response_model=UserOut)
def disable_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(require_admin),
) -> User:
    """Disable a user (``is_active = False``); an admin cannot disable themselves."""
    logger.info("POST /admin/users/%s/disable", user_id)
    if user_id == current_admin.id:
        raise HTTPException(status_code=400, detail="An admin cannot disable themselves")
    return user_service.set_active(db, user_id, False)


@router.delete("/users/{user_id}", response_model=UserOut)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(require_admin),
) -> User:
    """Soft-delete a user; an admin cannot delete themselves."""
    logger.info("DELETE /admin/users/%s", user_id)
    if user_id == current_admin.id:
        raise HTTPException(status_code=400, detail="An admin cannot delete themselves")
    return user_service.delete_user(db, user_id)
