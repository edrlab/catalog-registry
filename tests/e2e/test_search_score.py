"""The score of the real application, through the real HTTP path.

`tests/search_cases.py` says what search returns "today" and the score page is built from that.
This runs every search through the running app and scores what actually came back, so the
number on the page is a measurement and not a promise: if search changes, this changes with it.
"""

import pytest
from httpx import AsyncClient

from tests.search_cases import QUALITY_CASES
from tests.search_helpers import assert_valid_catalog, assert_valid_feed
from tests.search_quality import score_all, summarise, today_titles

pytestmark = pytest.mark.e2e


async def titles_for(client: AsyncClient, query: str) -> list[str]:
    response = await client.get("/search", params={"query": query})
    assert response.status_code == 200, query
    return [c["metadata"]["title"] for c in response.json()["catalogs"]]


async def test_the_app_scores_exactly_what_the_page_says(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    live = {case.query: await titles_for(client, case.query) for case in QUALITY_CASES}

    measured = summarise(score_all(live))
    documented = summarise(score_all(today_titles()))

    assert measured == documented, (
        f"search scores {measured.mean_score:.3f}, the page says {documented.mean_score:.3f}: "
        "if the change is intended, update tests/search_cases.py and run `make search-cases`"
    )


async def test_every_search_returns_what_the_page_says_for_it_and_a_valid_feed(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    """One pass over every search: what a client sees (titles in order, the total), and every
    response a valid feed of valid catalogs (R7). A regression names the queries."""
    expected = today_titles()
    wrong: dict[str, tuple[list[str], list[str]]] = {}
    for case in QUALITY_CASES:
        response = await client.get("/search", params={"query": case.query})
        assert response.status_code == 200, case.query
        body = response.json()
        assert_valid_feed(body)
        for catalog in body["catalogs"]:
            assert_valid_catalog(catalog)
        titles = [c["metadata"]["title"] for c in body["catalogs"]]
        if titles != expected[case.query] or body["metadata"]["numberOfItems"] != len(titles):
            wrong[case.query] = (titles, expected[case.query])

    assert wrong == {}
