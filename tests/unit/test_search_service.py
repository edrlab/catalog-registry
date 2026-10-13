"""The search service, against an in-memory fake searcher: no database, no framework.

The service's job is small and easy to get subtly wrong: parse, decide whether the database is
needed at all, translate a page number into a limit and an offset, and hand the page to the
renderer. Everything here is observable through what the fake was asked.
"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest

from registry.domain.restrictions import NO_RESTRICTIONS, Restrictions
from registry.domain.search_query import PAGE_SIZE, ParsedQuery
from registry.repositories.protocols import SearchPage
from registry.services.search_service import resolve_search

pytestmark = pytest.mark.unit

BASE = "https://example.org"


class FakeSearcher:
    """Satisfies `registry.repositories.protocols.CatalogSearcher`. Records what it was asked."""

    def __init__(self, total: int = 0) -> None:
        self.total = total
        self.calls: list[tuple[ParsedQuery, int, int]] = []
        self.restrictions: list[Restrictions] = []

    async def search_catalogs(
        self,
        query: ParsedQuery,
        *,
        limit: int,
        offset: int,
        restrictions: Restrictions = NO_RESTRICTIONS,
    ) -> SearchPage:
        self.calls.append((query, limit, offset))
        self.restrictions.append(restrictions)
        return SearchPage(total=self.total, catalogs=())


class FakeOpener:
    """A factory like `app.state.open_catalog_searcher`, counting how often it was opened."""

    def __init__(self, searcher: FakeSearcher) -> None:
        self.searcher = searcher
        self.opened = 0
        self.closed = 0

    def __call__(self) -> AbstractAsyncContextManager[FakeSearcher]:
        @asynccontextmanager
        async def open_searcher() -> AsyncIterator[FakeSearcher]:
            self.opened += 1
            try:
                yield self.searcher
            finally:
                self.closed += 1

        return open_searcher()


@pytest.mark.parametrize("query", [None, "", "   ", "-paris", '""', "OR"])
async def test_a_query_with_nothing_to_find_never_opens_the_searcher(query: str | None) -> None:
    opener = FakeOpener(FakeSearcher())

    feed = await resolve_search(opener, query=query, page=1, base_url=BASE)

    assert opener.opened == 0
    assert feed["catalogs"] == []
    assert feed["metadata"]["numberOfItems"] == 0


async def test_a_real_query_opens_the_searcher_once_and_closes_it() -> None:
    opener = FakeOpener(FakeSearcher())

    await resolve_search(opener, query="paris", page=1, base_url=BASE)

    assert (opener.opened, opener.closed) == (1, 1)


@pytest.mark.parametrize(("page", "offset"), [(1, 0), (2, 50), (3, 100), (1000, 49_950)])
async def test_the_page_number_becomes_a_limit_and_an_offset(page: int, offset: int) -> None:
    searcher = FakeSearcher()

    await resolve_search(FakeOpener(searcher), query="paris", page=page, base_url=BASE)

    ((_, limit, asked_offset),) = searcher.calls
    assert (limit, asked_offset) == (PAGE_SIZE, offset)
    assert PAGE_SIZE == 50


async def test_the_parsed_query_reaches_the_searcher_unfolded() -> None:
    searcher = FakeSearcher()

    await resolve_search(FakeOpener(searcher), query='Suiße -"x y" "a b"', page=1, base_url=BASE)

    ((parsed, _, _),) = searcher.calls
    assert parsed.positives == ("Suiße", '"a b"')
    assert parsed.negatives == ('"x y"',)


async def test_the_total_and_the_paging_links_come_from_the_page_the_searcher_returned() -> None:
    feed = await resolve_search(
        FakeOpener(FakeSearcher(total=80)), query="paris", page=1, base_url=BASE
    )

    assert feed["metadata"] == {
        "title": "Search results",
        "numberOfItems": 80,
        "itemsPerPage": 50,
        "currentPage": 1,
    }
    rels = {link["rel"] for link in feed["links"]}
    assert {"self", "search", "first", "next"} <= rels
    assert "previous" not in rels


@pytest.mark.parametrize("matches", [101, 120, 50_000])
async def test_the_total_is_capped_at_100_and_there_is_no_third_page(matches: int) -> None:
    first = await resolve_search(
        FakeOpener(FakeSearcher(total=matches)), query="paris", page=1, base_url=BASE
    )
    second = await resolve_search(
        FakeOpener(FakeSearcher(total=matches)), query="paris", page=2, base_url=BASE
    )

    assert first["metadata"]["numberOfItems"] == second["metadata"]["numberOfItems"] == 100
    assert "next" in {link["rel"] for link in first["links"]}
    assert "next" not in {link["rel"] for link in second["links"]}
    assert "previous" in {link["rel"] for link in second["links"]}


async def test_fewer_matches_than_the_cap_are_reported_as_they_are() -> None:
    feed = await resolve_search(
        FakeOpener(FakeSearcher(total=99)), query="paris", page=1, base_url=BASE
    )

    assert feed["metadata"]["numberOfItems"] == 99


async def test_a_searcher_that_raises_still_closes_and_the_error_reaches_the_caller() -> None:
    class Boom(FakeSearcher):
        async def search_catalogs(self, query, *, limit, offset, restrictions=NO_RESTRICTIONS):  # type: ignore[no-untyped-def]
            raise RuntimeError("database went away")

    opener = FakeOpener(Boom())

    with pytest.raises(RuntimeError, match="went away"):
        await resolve_search(opener, query="paris", page=1, base_url=BASE)

    assert opener.closed == 1
