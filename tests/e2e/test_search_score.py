"""The score of the real application, through the real HTTP path.

`tests/search_cases.py` says what search returns "today" and the score page is built from that.
This runs every search through the running app and scores what actually came back, so the
number on the page is a measurement and not a promise: if search changes, this changes with it.
"""

import pytest
from httpx import AsyncClient

from tests.search_cases import QUALITY_CASES
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


async def test_every_search_returns_what_the_page_says_for_it(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    """The same check one search at a time, so a regression names the query."""
    expected = today_titles()
    wrong = {
        case.query: (await titles_for(client, case.query), expected[case.query])
        for case in QUALITY_CASES
        if await titles_for(client, case.query) != expected[case.query]
    }

    assert wrong == {}
