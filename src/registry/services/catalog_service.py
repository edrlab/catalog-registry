"""Single-catalog reads."""

import uuid
from typing import Any

from registry.rendering.catalog_renderer import render_catalog
from registry.repositories.protocols import CatalogLoader


async def resolve_catalog_detail(
    loader: CatalogLoader, *, catalog_id: uuid.UUID, base_url: str
) -> dict[str, Any]:
    """Raises `NotFoundError` when the catalog does not exist."""
    return render_catalog(await loader.load_catalog_by_id(catalog_id), base_url=base_url)
