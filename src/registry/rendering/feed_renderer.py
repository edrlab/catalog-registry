"""The top-level feed document."""

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode

from registry.core.constants import OPDS_CATALOG_MEDIA_TYPE
from registry.domain.catalog_view import CatalogLike
from registry.domain.restrictions import NO_RESTRICTIONS, Restrictions
from registry.rendering.catalog_renderer import render_catalog
from registry.rendering.search_renderer import build_search_template_link

#: What the feed calls itself, matching `demo/index.json` and the `metadata.title` of
#: `data/recommended.json`, both of which name this exact document. It describes the
#: document's contents (the recommended catalogs), not the service that serves it; the
#: service's own name is the FastAPI `title` in `main.py`, which appears only in the OpenAPI
#: schema and never in an OPDS response.
FEED_TITLE = "Recommended Catalogs"


def build_self_link(base_url: str, restrictions: Restrictions = NO_RESTRICTIONS) -> dict[str, Any]:
    """`feed.schema.json` requires at least one link, and one of them to be `self`."""
    restrict = restrictions.query_value
    query = f"?{urlencode({'restrict': restrict}, safe=',')}" if restrict else ""
    return {
        "href": f"{base_url.rstrip('/')}/{query}",
        "type": OPDS_CATALOG_MEDIA_TYPE,
        "rel": "self",
    }


def render_feed(
    catalogs: Sequence[CatalogLike],
    *,
    base_url: str,
    restrictions: Restrictions = NO_RESTRICTIONS,
) -> dict[str, Any]:
    """the top-level feed does not paginate, so there is no `itemsPerPage`."""
    return {
        "metadata": {"title": FEED_TITLE, "numberOfItems": len(catalogs)},
        "links": [
            build_self_link(base_url, restrictions),
            build_search_template_link(base_url, restrictions),
        ],
        "catalogs": [render_catalog(catalog, base_url=base_url) for catalog in catalogs],
    }
