"""The search table of the test plan, again through the real HTTP path.

Same rows as `tests/integration/test_search_table.py` (which also pins the tier of each match);
here the answer is what a client sees: titles in order, and every response a valid feed (R7).
"""

import pytest
from httpx import AsyncClient

from tests.search_cases import CASES
from tests.search_helpers import assert_valid_catalog, assert_valid_feed

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(("query", "expected"), CASES, ids=[f"{q or '(empty)'}" for q, _ in CASES])
async def test_the_endpoint_returns_the_planned_catalogs_in_the_planned_order(
    client: AsyncClient,
    searchable_catalogs: None,
    query: str,
    expected: list[tuple[str, int]],
) -> None:
    response = await client.get("/search", params={"query": query})

    assert response.status_code == 200
    body = response.json()
    assert_valid_feed(body)
    for catalog in body["catalogs"]:
        assert_valid_catalog(catalog)
    assert [c["metadata"]["title"] for c in body["catalogs"]] == [title for title, _ in expected]
    assert body["metadata"]["numberOfItems"] == len(expected)
