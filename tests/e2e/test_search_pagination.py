"""Search paging over the real ASGI path (ADR-054): 50 per page, a deterministic order.

120 synthetic catalogs match one word, in three groups chosen so the order has to use every
term of `tier, score DESC, created_at DESC, id`:

* 50 titled "Zeppelin ..." (word match, label A, the highest score), in pairs sharing a
  `created_at` so only `id` can order them;
* 40 with the city Zeppelin (word match, label C, a lower score);
* 30 titled "Zeppelins ..." (trigram-only: the word is not `zeppelin`, tier 2).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import import_catalog_document
from tests.search_helpers import assert_valid_feed

pytestmark = pytest.mark.e2e

TITLE_MATCHES = 50
CITY_MATCHES = 40
TRIGRAM_MATCHES = 30
TOTAL = TITLE_MATCHES + CITY_MATCHES + TRIGRAM_MATCHES
BASE = "http://testserver"
QUERY = "zeppelin"


def document(title: str, href: str, city: str | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"title": title, "kind": ["public"]}
    if city:
        metadata["city"] = city
    return {
        "metadata": metadata,
        "links": [{"href": href, "type": "application/opds+json", "rel": "catalog"}],
    }


@pytest.fixture
async def zeppelin_ids(db_session: AsyncSession) -> list[str]:
    """The ids the search must return, in the order it must return them."""
    epoch = datetime(2030, 1, 1, tzinfo=UTC)
    groups: list[list[tuple[datetime, str]]] = [[], [], []]
    spec = [
        (0, TITLE_MATCHES, lambda n: (f"Zeppelin shelf {n}", None)),
        (1, CITY_MATCHES, lambda n: (f"Dirigible shelf {n}", "Zeppelin")),
        (2, TRIGRAM_MATCHES, lambda n: (f"Zeppelins shelf {n}", None)),
    ]
    for group, count, make in spec:
        for n in range(count):
            title, city = make(n)
            # Pairs share a timestamp, so within a pair only the id breaks the tie.
            ordered_at = epoch + timedelta(minutes=n // 2 + group * 1000)
            catalog, _ = await import_catalog_document(
                db_session,
                document(title, f"https://zeppelin.example/{group}/{n}", city),
                recommended=False,
                ordered_at=ordered_at,
            )
            groups[group].append((ordered_at, str(catalog.id)))
    await db_session.flush()
    expected: list[str] = []
    for group in groups:
        # created_at descending, then id ascending.
        by_time: dict[datetime, list[str]] = {}
        for ordered_at, catalog_id in group:
            by_time.setdefault(ordered_at, []).append(catalog_id)
        for ordered_at in sorted(by_time, reverse=True):
            expected += sorted(by_time[ordered_at])
    assert len(expected) == TOTAL
    return expected


async def page_of(client: AsyncClient, page: int) -> dict[str, Any]:
    response = await client.get("/search", params={"query": QUERY, "page": page})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert_valid_feed(body)
    return body


def ids_of(body: dict[str, Any]) -> list[str]:
    return [c["metadata"]["identifier"].removeprefix("urn:uuid:") for c in body["catalogs"]]


async def test_pages_split_the_matches_fifty_at_a_time_in_the_documented_order(
    client: AsyncClient, zeppelin_ids: list[str]
) -> None:
    first, second, third = [await page_of(client, n) for n in (1, 2, 3)]

    assert [len(first["catalogs"]), len(second["catalogs"]), len(third["catalogs"])] == [50, 50, 20]
    assert ids_of(first) + ids_of(second) + ids_of(third) == zeppelin_ids


async def test_no_catalog_appears_on_two_pages_and_none_is_skipped(
    client: AsyncClient, zeppelin_ids: list[str]
) -> None:
    seen: list[str] = []
    for n in (1, 2, 3):
        seen += ids_of(await page_of(client, n))

    assert len(seen) == len(set(seen)) == TOTAL
    assert set(seen) == set(zeppelin_ids)


async def test_tiers_come_in_order_across_the_page_boundaries(
    client: AsyncClient, zeppelin_ids: list[str]
) -> None:
    """Title matches (50) fill page 1, city matches follow, trigram-only matches come last."""
    titles: list[str] = []
    for n in (1, 2, 3):
        titles += [c["metadata"]["title"] for c in (await page_of(client, n))["catalogs"]]

    assert all(t.startswith("Zeppelin shelf") for t in titles[:TITLE_MATCHES])
    middle = titles[TITLE_MATCHES : TITLE_MATCHES + CITY_MATCHES]
    assert all(t.startswith("Dirigible shelf") for t in middle)
    assert all(t.startswith("Zeppelins shelf") for t in titles[TITLE_MATCHES + CITY_MATCHES :])


async def test_the_same_page_twice_gives_the_same_answer(
    client: AsyncClient, zeppelin_ids: list[str]
) -> None:
    assert ids_of(await page_of(client, 2)) == ids_of(await page_of(client, 2))


@pytest.mark.parametrize("page", [1, 2, 3, 4, 1000])
async def test_number_of_items_is_the_total_on_every_page_including_past_the_end(
    client: AsyncClient, zeppelin_ids: list[str], page: int
) -> None:
    body = await page_of(client, page)

    assert body["metadata"]["numberOfItems"] == TOTAL
    assert body["metadata"]["currentPage"] == page
    assert body["metadata"]["itemsPerPage"] == 50


async def test_pages_past_the_end_are_empty_and_page_1000_works(
    client: AsyncClient, zeppelin_ids: list[str]
) -> None:
    assert (await page_of(client, 4))["catalogs"] == []
    assert (await page_of(client, 1000))["catalogs"] == []


@pytest.mark.parametrize(
    ("page", "present", "absent"),
    [
        (1, {"self", "search", "first", "next"}, {"previous"}),
        (2, {"self", "search", "first", "previous", "next"}, set()),
        (3, {"self", "search", "first", "previous"}, {"next"}),
        (4, {"self", "search", "first", "previous"}, {"next"}),
    ],
)
async def test_the_paging_links_are_present_exactly_when_they_should_be(
    client: AsyncClient, zeppelin_ids: list[str], page: int, present: set[str], absent: set[str]
) -> None:
    rels = {link["rel"]: link["href"] for link in (await page_of(client, page))["links"]}

    assert present <= set(rels)
    assert not absent & set(rels)


async def test_the_paging_links_point_at_the_right_pages(
    client: AsyncClient, zeppelin_ids: list[str]
) -> None:
    rels = {link["rel"]: link["href"] for link in (await page_of(client, 2))["links"]}

    assert rels["self"] == f"{BASE}/search?query={QUERY}&page=2"
    assert rels["first"] == f"{BASE}/search?query={QUERY}"
    assert rels["previous"] == f"{BASE}/search?query={QUERY}"
    assert rels["next"] == f"{BASE}/search?query={QUERY}&page=3"


async def test_every_paging_link_can_be_followed(
    client: AsyncClient, zeppelin_ids: list[str]
) -> None:
    """Follow `next` from page 1 to the end: the links alone reach every catalog."""
    seen: list[str] = []
    href: str | None = f"{BASE}/search?query={QUERY}"
    while href:
        response = await client.get(href)
        assert response.status_code == 200
        body = response.json()
        seen += ids_of(body)
        href = next((link["href"] for link in body["links"] if link["rel"] == "next"), None)

    assert seen == zeppelin_ids


@pytest.mark.parametrize("page", [0, -5, 1001, 10**9])
async def test_a_page_outside_the_range_is_a_422(
    client: AsyncClient, zeppelin_ids: list[str], page: int
) -> None:
    response = await client.get("/search", params={"query": QUERY, "page": page})

    # FastAPI's own validation error: `application/json`, not problem+json (observed; the
    # registry's handlers cover RegistryError, HTTPException and unexpected errors only).
    assert response.status_code == 422


async def test_a_page_that_is_not_a_number_is_a_422(client: AsyncClient) -> None:
    assert (await client.get("/search", params={"query": "x", "page": "two"})).status_code == 422
