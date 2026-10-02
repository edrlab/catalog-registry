"""The search results feed (ADR-054).

Catalogs are rendered by `render_catalog`, the same projection the top-level feed uses, so a
search result can never carry a field the feed would not.
"""

from typing import Any
from urllib.parse import quote, urlencode

from registry.core.constants import OPDS_JSON_MEDIA_TYPE
from registry.domain.search_query import MAX_PAGE
from registry.rendering.catalog_renderer import render_catalog
from registry.repositories.protocols import SearchPage

SEARCH_TITLE = "Search results"


def build_search_template_link(base_url: str) -> dict[str, Any]:
    """The templated `search` link, on the top-level feed and on every results page."""
    return {
        "href": f"{base_url.rstrip('/')}/search{{?query}}",
        "type": OPDS_JSON_MEDIA_TYPE,
        "rel": "search",
        "templated": True,
    }


def _page_href(base_url: str, query: str, page: int) -> str:
    """`page` is left out for page 1, so `self` on the first page is the plain query URL."""
    params: dict[str, str | int] = {"query": query}
    if page > 1:
        params["page"] = page
    return f"{base_url.rstrip('/')}/search?{urlencode(params, quote_via=quote)}"


def _link(rel: str, href: str) -> dict[str, Any]:
    return {"href": href, "type": OPDS_JSON_MEDIA_TYPE, "rel": rel}


def render_search_results(
    result: SearchPage, *, query: str, page: int, page_size: int, base_url: str
) -> dict[str, Any]:
    """`previous` and `next` appear only when there is a page to go to.

    `next` stops at `MAX_PAGE`: the route answers 422 beyond it, so a link there would be dead."""
    links = [
        _link("self", _page_href(base_url, query, page)),
        build_search_template_link(base_url),
        _link("first", _page_href(base_url, query, 1)),
    ]
    if page > 1:
        links.append(_link("previous", _page_href(base_url, query, page - 1)))
    if page < MAX_PAGE and page * page_size < result.total:
        links.append(_link("next", _page_href(base_url, query, page + 1)))
    return {
        "metadata": {
            "title": SEARCH_TITLE,
            "numberOfItems": result.total,
            "itemsPerPage": page_size,
            "currentPage": page,
        },
        "links": links,
        "catalogs": [render_catalog(catalog, base_url=base_url) for catalog in result.catalogs],
    }
