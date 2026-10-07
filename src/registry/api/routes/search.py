"""`GET /search`. The concrete searcher is wired in `main.py` and reached through `app.state`."""

from typing import Any

from fastapi import APIRouter, Query, Request

from registry.api.responses import OPDSResponse
from registry.domain.search_query import MAX_PAGE
from registry.schemas.feed import FeedResponse
from registry.services.search_service import resolve_search

router = APIRouter(tags=["search"])


@router.get(
    "/search",
    response_model=FeedResponse,
    response_class=OPDSResponse,
    response_model_exclude_none=True,
    summary="Search the catalogs, 50 per page",
)
async def read_search_results(
    request: Request,
    query: str | None = None,
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
) -> dict[str, Any]:
    return await resolve_search(
        request.app.state.open_catalog_searcher,
        query=query,
        page=page,
        base_url=request.app.state.settings.base_url,
    )
