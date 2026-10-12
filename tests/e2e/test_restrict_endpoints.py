"""`restrict` on `GET /` and `GET /search`, over the real ASGI path (issue #17).

Both data sets are loaded: `recommended.json` (every catalog `open`, only Project Gutenberg with
an OPDS 2.0 link) and `libraries.json` (`public`, most with OPDS). The expected result of each
search is worked out from the *unrestricted* response by reading its links and kinds, so the
check is independent of the rule as the database states it.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from registry.core.constants import OPDS_JSON_MEDIA_TYPE
from tests.search_helpers import assert_valid_feed

pytestmark = pytest.mark.e2e

ANY = {"Accept-Language": "*"}
QUERIES = ["bibliotheque", "belgique", "ebooks", "france", "valais", "paris", "gutenberg", "liber"]


def titles(feed: dict[str, Any]) -> list[str]:
    return [c["metadata"]["title"] for c in feed["catalogs"]]


def has_opds2(catalog: dict[str, Any]) -> bool:
    return any(
        link["rel"] == "catalog" and link.get("type") == OPDS_JSON_MEDIA_TYPE
        for link in catalog["links"]
    )


def is_open(catalog: dict[str, Any]) -> bool:
    return "open" in catalog["metadata"]["kind"]


async def search(client: AsyncClient, query: str, restrict: str | None = None) -> dict[str, Any]:
    params = {"query": query, **({"restrict": restrict} if restrict is not None else {})}
    response = await client.get("/search", params=params)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert_valid_feed(body)
    return body


async def test_the_feed_without_restrict_is_unchanged(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    plain = (await client.get("/", headers=ANY)).json()
    empty = (await client.get("/", params={"restrict": ""}, headers=ANY)).json()

    assert plain == empty
    assert {link["rel"]: link["href"] for link in plain["links"]}["self"].endswith("testserver/")


async def test_opds_keeps_only_catalogs_with_an_opds2_catalog_link(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    feed = (await client.get("/", params={"restrict": "opds"}, headers=ANY)).json()

    assert_valid_feed(feed)
    assert titles(feed) == ["Project Gutenberg"]
    assert feed["metadata"]["numberOfItems"] == 1


async def test_no_open_empties_a_feed_whose_catalogs_are_all_open(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    feed = (await client.get("/", params={"restrict": "no-open"}, headers=ANY)).json()

    assert_valid_feed(feed)
    assert feed["catalogs"] == []
    assert feed["metadata"]["numberOfItems"] == 0


async def test_the_unscoped_fallback_and_a_restriction_work_together(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    """No language header: Gutenberg and Librivox. Only Gutenberg has an OPDS 2.0 feed."""
    feed = (await client.get("/", params={"restrict": "opds"})).json()

    assert titles(feed) == ["Project Gutenberg"]


async def test_the_feed_links_keep_the_restriction(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    feed = (await client.get("/", params={"restrict": "no-open,opds"}, headers=ANY)).json()
    hrefs = {link["rel"]: link["href"] for link in feed["links"]}

    assert hrefs["self"] == "http://testserver/?restrict=opds,no-open"
    assert hrefs["search"] == "http://testserver/search?restrict=opds,no-open{&query}"


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize(
    ("restrict", "keep"),
    [
        ("opds", has_opds2),
        ("no-open", lambda c: not is_open(c)),
        ("opds,no-open", lambda c: has_opds2(c) and not is_open(c)),
    ],
)
async def test_a_restricted_search_is_the_unrestricted_one_with_catalogs_removed(
    client: AsyncClient, searchable_catalogs: None, query: str, restrict: str, keep: Any
) -> None:
    """Same catalogs, same order, same total; restrictions never rank."""
    plain = await search(client, query)
    restricted = await search(client, query, restrict)

    expected = [c["metadata"]["title"] for c in plain["catalogs"] if keep(c)]
    assert titles(restricted) == expected
    assert restricted["metadata"]["numberOfItems"] == len(expected)


async def test_a_search_link_set_keeps_the_restriction(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    body = await search(client, "bibliotheque", "opds")
    hrefs = {link["rel"]: link["href"] for link in body["links"]}

    assert hrefs["self"] == "http://testserver/search?query=bibliotheque&restrict=opds"
    assert hrefs["first"] == hrefs["self"]
    assert hrefs["search"] == "http://testserver/search?restrict=opds{&query}"


async def test_a_restriction_that_removes_everything_is_an_empty_valid_feed(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    body = await search(client, "ebooks", "no-open")

    assert body["catalogs"] == []
    assert body["metadata"]["numberOfItems"] == 0


@pytest.mark.parametrize("path", ["/", "/search?query=paris"])
@pytest.mark.parametrize("value", ["opsd", "OPDS", "opds,", "all", "opds, no-open"])
async def test_an_unknown_restriction_is_a_422(client: AsyncClient, path: str, value: str) -> None:
    response = await client.get(path, params={"restrict": value})

    assert response.status_code == 422
