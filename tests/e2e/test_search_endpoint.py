"""`GET /search` over the real ASGI path (ADR-054). Ranking cases are in the test table suite."""

from typing import Any, NoReturn

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from registry.core.constants import OPDS_CATALOG_MEDIA_TYPE
from registry.core.schema_validation import build_schema_validator
from tests.search_helpers import assert_valid_feed

pytestmark = pytest.mark.e2e


async def search_titles(client: AsyncClient, query: str, **params: Any) -> list[str]:
    response = await client.get("/search", params={"query": query, **params})
    assert response.status_code == 200
    return [catalog["metadata"]["title"] for catalog in response.json()["catalogs"]]


async def test_a_search_finds_a_catalog_by_a_place_name_in_another_language(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    response = await client.get("/search", params={"query": "wallis"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(OPDS_CATALOG_MEDIA_TYPE)
    body = response.json()
    # The exact match first; Lirtuel (Wallonia) follows as a trigram match at threshold 0.5.
    assert [c["metadata"]["title"] for c in body["catalogs"]] == ["Médiathèque Valais", "Lirtuel"]
    assert body["metadata"] == {
        "title": "Search results",
        "numberOfItems": 2,
        "itemsPerPage": 50,
        "currentPage": 1,
    }
    assert_valid_feed(body)


async def test_every_result_validates_against_the_catalog_schema(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    body = (await client.get("/search", params={"query": "bibliotheque"})).json()
    validator = build_schema_validator("catalog.schema.json")

    assert body["catalogs"]
    for catalog in body["catalogs"]:
        errors = sorted(validator.iter_errors(catalog), key=str)
        assert not errors, [catalog["metadata"]["title"], [e.message for e in errors]]


async def test_the_links_of_a_single_page_result(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    links = (await client.get("/search", params={"query": "numérique de paris"})).json()["links"]

    by_rel = {link["rel"]: link for link in links}
    assert set(by_rel) == {"self", "search", "first"}
    assert by_rel["self"]["href"] == "http://testserver/search?query=num%C3%A9rique%20de%20paris"
    assert by_rel["first"]["href"] == by_rel["self"]["href"]
    assert by_rel["search"] == {
        "href": "http://testserver/search{?query}",
        "type": "application/opds+json",
        "rel": "search",
        "templated": True,
    }


async def test_a_page_past_the_end_is_empty_but_still_reports_the_total(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    body = (await client.get("/search", params={"query": "wallis", "page": 2})).json()

    assert body["catalogs"] == []
    assert body["metadata"]["numberOfItems"] == 2
    assert body["metadata"]["currentPage"] == 2
    rels = {link["rel"]: link["href"] for link in body["links"]}
    assert rels["self"].endswith("&page=2")
    assert rels["previous"] == "http://testserver/search?query=wallis"
    assert "next" not in rels
    assert_valid_feed(body)


@pytest.mark.parametrize("page", [0, -1, 1001])
async def test_a_page_outside_1_to_1000_is_rejected(client: AsyncClient, page: int) -> None:
    assert (await client.get("/search", params={"query": "x", "page": page})).status_code == 422


@pytest.mark.parametrize("params", [{}, {"query": ""}, {"query": "   "}, {"query": "-paris"}])
async def test_an_empty_query_returns_an_empty_feed_and_never_opens_a_session(
    client: AsyncClient, app: FastAPI, params: dict[str, str]
) -> None:
    def raising_factory() -> NoReturn:
        raise AssertionError("an empty query must not touch the database")

    app.state.open_catalog_searcher = raising_factory

    response = await client.get("/search", params=params)

    assert response.status_code == 200
    body = response.json()
    assert body["catalogs"] == []
    assert body["metadata"]["numberOfItems"] == 0
    assert_valid_feed(body)


async def test_the_top_level_feed_advertises_the_search_template(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    body = (await client.get("/")).json()

    search = next(link for link in body["links"] if link["rel"] == "search")
    assert search["href"] == "http://testserver/search{?query}"
    assert search["templated"] is True
    assert_valid_feed(body)


@pytest.mark.parametrize(
    "header", ["fr", "en-US,en;q=0.9", "ja", "de-DE,de;q=0.9,fr;q=0.5", "*", "xx-garbage;;;"]
)
async def test_search_ignores_accept_language(
    client: AsyncClient, searchable_catalogs: None, header: str
) -> None:
    """ADR-044: the top-level feed filters and orders by `Accept-Language`, search does not.
    A reader who asks for `tv5` or `liber` in a French browser still finds Italian catalogs;
    a language filter is a later, explicit parameter."""
    plain = await client.get("/search", params={"query": "bibliotheque"})
    with_header = await client.get(
        "/search", params={"query": "bibliotheque"}, headers={"Accept-Language": header}
    )

    assert with_header.status_code == 200
    assert with_header.json() == plain.json()
    assert plain.json()["catalogs"], "the comparison would be vacuous on an empty result"


async def test_search_finds_a_catalog_whose_language_the_header_would_have_excluded(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    """`Liber Liber` declares Italian only; the top-level feed hides it from a French reader."""
    feed = await client.get("/", headers={"Accept-Language": "fr"})
    found = await client.get(
        "/search", params={"query": "liber liber"}, headers={"Accept-Language": "fr"}
    )

    assert "Liber Liber" not in [c["metadata"]["title"] for c in feed.json()["catalogs"]]
    assert "Liber Liber" in [c["metadata"]["title"] for c in found.json()["catalogs"]]
