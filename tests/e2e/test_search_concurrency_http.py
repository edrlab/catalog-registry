"""Thirty searches at once on the real app and its search pool.

Each request checks out its own connection from the dedicated pool and runs one statement. Under
load the pages must be the ones a lone request gets, and every connection must come back: a leak
would show as `checkedout()` above zero once the requests are done.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from registry.core.config import Settings
from registry.main import create_app
from tests.search_helpers import HANG_GUARD_SECONDS, assert_valid_feed, committed_catalogs

pytestmark = pytest.mark.e2e

CONCURRENT = 30
CATALOGS = 12


def documents() -> list[dict[str, Any]]:
    return [
        {
            "metadata": {
                "title": f"{'okapi' if n % 2 else 'pangolin'} shelf {n:02d}",
                "kind": ["public"],
                "supportedLanguages": ["fr", "en"],
            },
            "links": [
                {"href": f"https://load.example/{n}", "rel": "catalog"},
                {"href": f"https://load.example/{n}/icon", "rel": "icon", "type": "image/png"},
            ],
        }
        for n in range(CATALOGS)
    ]


@pytest.fixture
async def wired_app(settings: Settings, migrated_database: str) -> AsyncIterator[FastAPI]:
    built = create_app(settings)
    async with built.router.lifespan_context(built):
        yield built


async def test_thirty_concurrent_searches_return_the_right_pages_and_leak_nothing(
    wired_app: FastAPI, migrated_database: str
) -> None:
    # "okapi shelf" also returns the pangolin shelves: "shelf" alone is a close typo hit.
    queries = ["okapi", "pangolin", "okapi shelf", "xyzzy", "pangolin -okapi", "shelf"]
    async with (
        committed_catalogs(migrated_database, documents()) as ids,
        AsyncClient(transport=ASGITransport(app=wired_app), base_url="http://testserver") as client,
    ):
        alone = {q: (await client.get("/search", params={"query": q})).json() for q in queries}
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            responses = await asyncio.gather(
                *(
                    client.get("/search", params={"query": queries[n % len(queries)]})
                    for n in range(CONCURRENT)
                )
            )

    for n, response in enumerate(responses):
        query = queries[n % len(queries)]
        assert response.status_code == 200, query
        body = response.json()
        assert body == alone[query], query
        assert_valid_feed(body)
    # By this test's own ids: another test's committed rows must not change the answer.
    mine = {f"urn:uuid:{i}" for i in ids}
    okapi = {f"urn:uuid:{i}" for n, i in enumerate(ids) if n % 2}
    expected = {
        "okapi": okapi,
        "pangolin": mine - okapi,
        "okapi shelf": mine,
        "xyzzy": set(),
        "pangolin -okapi": mine - okapi,
        "shelf": mine,
    }
    for query in queries:
        got = {c["metadata"]["identifier"] for c in alone[query].get("catalogs", [])}
        assert got & mine == expected[query], query
    pool = wired_app.state.search_engine.sync_engine.pool
    assert pool.checkedout() == 0
    assert pool.checkedin() >= 1


async def test_the_search_pool_is_bounded_and_queues_instead_of_failing(
    settings: Settings, migrated_database: str
) -> None:
    """30 requests against a pool of 2 + 3 overflow: it grows past its size, never past its
    bound, and the requests it cannot serve at once wait instead of failing."""
    size, overflow = 2, 3
    app = create_app(
        settings.model_copy(update={"database_pool_size": size, "database_max_overflow": overflow})
    )
    in_use = high_water = 0

    def checkout(*_args: object) -> None:
        nonlocal in_use, high_water
        in_use += 1
        high_water = max(high_water, in_use)

    def checkin(*_args: object) -> None:
        nonlocal in_use
        in_use -= 1

    async with (
        app.router.lifespan_context(app),
        committed_catalogs(migrated_database, documents()),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        pool = app.state.search_engine.sync_engine.pool
        event.listen(pool, "checkout", checkout)
        event.listen(pool, "checkin", checkin)
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            responses = await asyncio.gather(
                *(client.get("/search", params={"query": "okapi"}) for _ in range(CONCURRENT))
            )

        assert {r.status_code for r in responses} == {200}
        assert size + 1 <= high_water <= size + overflow
        assert in_use == pool.checkedout() == 0
