"""`GET /catalogs/{catalog_id}`, one catalog.

This is what every synthesised `self` link points at, so it has to exist for the feed's own
output to be honest.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Request

from registry.api.responses import OPDSResponse
from registry.schemas.catalog import CatalogResponse
from registry.services.catalog_service import resolve_catalog_detail

router = APIRouter(tags=["catalogs"])


@router.get(
    "/catalogs/{catalog_id}",
    response_model=CatalogResponse,
    response_class=OPDSResponse,
    response_model_exclude_none=True,
    summary="One catalog",
)
async def read_catalog(request: Request, catalog_id: uuid.UUID) -> dict[str, Any]:
    """A malformed uuid is a 422 from FastAPI's own path validation; an unknown one is a
    404 raised by the service and rendered as problem+json."""
    async with request.app.state.open_catalog_reader() as reader:
        return await resolve_catalog_detail(
            reader, catalog_id=catalog_id, base_url=request.app.state.settings.base_url
        )
