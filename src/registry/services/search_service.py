"""Search: parse, query, render. An empty query does no database work (ADR-054)."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Final

from registry.domain.search_query import parse_search_query
from registry.rendering.search_renderer import render_search_results
from registry.repositories.protocols import CatalogSearcher, SearchPage

PAGE_SIZE: Final = 50


async def resolve_search(
    open_searcher: Callable[[], AbstractAsyncContextManager[CatalogSearcher]],
    *,
    query: str | None,
    page: int,
    base_url: str,
) -> dict[str, Any]:
    """Take a factory, not an open searcher, so the empty case never opens a session."""
    parsed = parse_search_query(query)
    if parsed.is_empty:
        result = SearchPage(total=0, catalogs=())
    else:
        async with open_searcher() as searcher:
            result = await searcher.search_catalogs(
                parsed, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
            )
    return render_search_results(
        result, query=query or "", page=page, page_size=PAGE_SIZE, base_url=base_url
    )
