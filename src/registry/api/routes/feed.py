"""`GET /` — the top-level feed.

The concrete repository is wired in `main.py`, the composition root, and reached through
`app.state`. `api` importing `repositories` is a boundary violation the architecture test
fails on — see `architecture/system/00-layers-and-boundaries.md` §1.
"""

from typing import Any

from fastapi import APIRouter, Header, Request

from registry.api.responses import OPDSResponse
from registry.schemas.feed import FeedResponse
from registry.services.feed_service import resolve_top_level_feed

router = APIRouter(tags=["feed"])


@router.get(
    "/",
    response_model=FeedResponse,
    response_class=OPDSResponse,
    response_model_exclude_none=True,
    summary="The recommended catalogs, filtered by Accept-Language",
)
async def read_top_level_feed(
    request: Request, accept_language: str | None = Header(default=None)
) -> dict[str, Any]:
    async with request.app.state.open_catalog_reader() as reader:
        return await resolve_top_level_feed(
            reader,
            accept_language=accept_language,
            base_url=request.app.state.settings.base_url,
        )
