"""Semantic search API routes.

Thin router: validate input (FastAPI/pydantic yields 422 on an empty query),
delegate the business logic to ``search_service``, and shape the JSON response.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.database.session import get_db
from app.models.user import User
from app.schemas.search import SearchRequest, SearchResponse, SearchResult
from app.services import search_service
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["search"])


@router.post("/search", response_model=SearchResponse)
def search(
    payload: SearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchResponse:
    """Semantic search over the caller's own + admin-global document chunks.

    Embeds the query, runs a tenant-scoped (DEFAULT-DENY) vector similarity
    search, and returns the top-k most-relevant chunks (configurable via
    ``top_k``), most relevant first. Another tenant's chunks are never candidates.
    Results carry no numeric score (same policy as chat sources).
    """
    logger.info(
        "POST /search query=%r top_k=%s owner_id=%s",
        payload.query,
        payload.top_k,
        current_user.id,
    )
    hits = search_service.search(
        db,
        query=payload.query,
        top_k=payload.top_k,
        owner_id=current_user.id,
        include_global=True,
    )
    results = [SearchResult(**hit) for hit in hits]
    return SearchResponse(query=payload.query, results=results)
