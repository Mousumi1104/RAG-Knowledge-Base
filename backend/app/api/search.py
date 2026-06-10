"""Semantic search API routes.

Thin router: validate input (FastAPI/pydantic yields 422 on an empty query),
delegate the business logic to ``search_service``, and shape the JSON response.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.search import SearchRequest, SearchResponse, SearchResult
from app.services import search_service
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["search"])


@router.post("/search", response_model=SearchResponse)
def search(
    payload: SearchRequest,
    db: Session = Depends(get_db),
) -> SearchResponse:
    """Semantic search over indexed, ACTIVE document chunks.

    Embeds the query, runs a vector similarity search, and returns the top-k
    most-relevant chunks (configurable via ``top_k``), most relevant first.
    """
    logger.info("POST /search query=%r top_k=%s", payload.query, payload.top_k)
    hits = search_service.search(db, query=payload.query, top_k=payload.top_k)
    results = [SearchResult(**hit) for hit in hits]
    return SearchResponse(query=payload.query, results=results)
