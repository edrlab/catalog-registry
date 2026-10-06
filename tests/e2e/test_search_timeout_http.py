"""The search pool through the real app: its own engine, its own limits (ADR-058 amended).

The app has three pools: the main one (readiness and the importers), the search pool (1 s timeout)
and the read pool of the feed and one catalog (10 s timeout, ADR-062). Each read kind has its
own, so one kind's limits and stuck connections cannot reach another's.

Unlike the rest of the e2e suite this does not rebind the app to the rollback fixture, because
the timeout and the read-only mode have to be proven on the wiring `main.py` builds: `create_app`
and its lifespan, against the migrated database. A second connection holds `ACCESS EXCLUSIVE` on
`catalog_search` so the search blocks; the lock is rolled back in a `finally` and nothing is
committed (the isolation tests that need rows commit them and delete them in a `finally`).
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text

from registry.core.config import Settings
from registry.core.constants import PROBLEM_JSON_MEDIA_TYPE
from registry.main import create_app
from registry.repositories.search_repository import SEARCH_STATEMENT_TIMEOUT_MS
from tests.search_helpers import (
    HANG_GUARD_SECONDS,
    assert_valid_catalog,
    assert_valid_feed,
    committed_catalogs,
    table_locked,
)

pytestmark = pytest.mark.e2e


@pytest.fixture
async def wired_app(settings: Settings, migrated_database: str) -> AsyncIterator[FastAPI]:
    built = create_app(settings)
    async with built.router.lifespan_context(built):
        yield built


@pytest.fixture
async def wired_client(wired_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=wired_app), base_url="http://testserver"
    ) as client:
        yield client


class Recorder:
    """What reaches the driver on one engine."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, _conn: object, _cursor: object, statement: str, *_a: object) -> None:
        self.statements.append(statement)


@dataclass
class PoolRecorders:
    main: Recorder
    read: Recorder
    search: Recorder


@pytest.fixture
def recorders(wired_app: FastAPI) -> Iterator[PoolRecorders]:
    """Statement recorders on the real app's three pools."""
    state = wired_app.state
    found = PoolRecorders(Recorder(), Recorder(), Recorder())
    pairs = [(state.engine, found.main), (state.read_engine, found.read)]
    pairs.append((state.search_engine, found.search))
    for engine, recorder in pairs:
        event.listen(engine.sync_engine, "before_cursor_execute", recorder)
    yield found
    for engine, recorder in pairs:
        event.remove(engine.sync_engine, "before_cursor_execute", recorder)


def test_the_production_timeout_is_one_second() -> None:
    assert SEARCH_STATEMENT_TIMEOUT_MS == 1000


async def test_a_blocked_search_answers_503_after_about_one_second(
    wired_client: AsyncClient, migrated_database: str
) -> None:
    async with table_locked(migrated_database):
        started = time.monotonic()
        response = await asyncio.wait_for(
            wired_client.get("/search", params={"query": "paris"}), HANG_GUARD_SECONDS
        )
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    body = response.json()
    assert body["status"] == 503
    assert body["title"] == "Service Unavailable"
    assert "too long" in body["detail"]
    assert body["type"] == "about:blank"
    # The real constant is enforced: roughly a second, not instant and not unbounded.
    assert 0.9 < elapsed < 5


async def test_the_service_recovers_when_the_lock_is_gone(
    wired_client: AsyncClient, migrated_database: str
) -> None:
    async with table_locked(migrated_database):
        blocked = await asyncio.wait_for(
            wired_client.get("/search", params={"query": "paris"}), HANG_GUARD_SECONDS
        )
        assert blocked.status_code == 503

    response = await wired_client.get("/search", params={"query": "paris"})

    assert response.status_code == 200
    assert_valid_feed(response.json())


async def test_a_timeout_does_not_poison_the_next_request_on_the_same_app(
    wired_client: AsyncClient, migrated_database: str
) -> None:
    async with table_locked(migrated_database):
        await asyncio.wait_for(
            wired_client.get("/search", params={"query": "paris"}), HANG_GUARD_SECONDS
        )

    # The feed has its own read pool; it must be unaffected.
    assert (await wired_client.get("/")).status_code == 200


async def test_the_search_session_runs_on_a_read_only_connection_with_the_one_second_timeout(
    wired_app: FastAPI,
) -> None:
    async with wired_app.state.open_catalog_searcher() as searcher:
        session = searcher._session  # the searcher's own session, not a copy
        assert await session.scalar(text("SHOW default_transaction_read_only")) == "on"
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        assert await session.scalar(text("SHOW statement_timeout")) == "1s"


async def test_the_three_pools_are_distinct_and_each_factory_is_bound_to_its_own(
    wired_app: FastAPI,
) -> None:
    state = wired_app.state
    pools = {
        state.engine.sync_engine.pool,
        state.read_engine.sync_engine.pool,
        state.search_engine.sync_engine.pool,
    }
    assert len(pools) == 3
    assert state.search_session_factory is not state.session_factory
    assert state.search_session_factory.kw["bind"] is state.search_engine
    assert state.session_factory.kw["bind"] is state.read_engine


# --- E. the feed and the catalog endpoint are untouched by the search pool's limits -----------

PROBE_TITLE = "Isolation probe"


def probe_document() -> dict[str, Any]:
    return {
        "metadata": {"title": PROBE_TITLE, "kind": ["public"], "color": "blue"},
        "links": [
            {
                "href": f"https://isolation.example/{uuid.uuid4()}",
                "type": "application/opds+json",
                "rel": "catalog",
            }
        ],
    }


async def test_the_feed_and_a_catalog_work_and_use_the_read_pool_only(
    wired_client: AsyncClient, migrated_database: str, recorders: PoolRecorders
) -> None:
    async with committed_catalogs(migrated_database, [probe_document()], recommended=True) as (
        catalog_id,
    ):
        feed = await wired_client.get("/")
        single = await wired_client.get(f"/catalogs/{catalog_id}")
        seen = (list(recorders.main.statements), list(recorders.read.statements))
        reached_search_pool = list(recorders.search.statements)

    assert feed.status_code == 200
    assert_valid_feed(feed.json())
    assert [c["metadata"]["title"] for c in feed.json()["catalogs"]] == [PROBE_TITLE]
    assert single.status_code == 200
    assert_valid_catalog(single.json())
    assert single.json()["metadata"]["title"] == PROBE_TITLE
    assert reached_search_pool == []
    reached_main_pool, reached_read_pool = seen
    assert reached_main_pool == []  # readiness and the importers only
    assert len(reached_read_pool) == 2  # one statement per request, nothing else


async def test_a_search_uses_the_search_pool_and_neither_of_the_others(
    wired_client: AsyncClient, migrated_database: str, recorders: PoolRecorders
) -> None:
    async with committed_catalogs(migrated_database, [probe_document()]):
        response = await wired_client.get("/search", params={"query": "isolation"})
        reached = (
            list(recorders.main.statements),
            list(recorders.read.statements),
            list(recorders.search.statements),
        )

    assert response.status_code == 200
    assert [c["metadata"]["title"] for c in response.json()["catalogs"]] == [PROBE_TITLE]
    assert reached[0] == []
    assert reached[1] == []
    assert len(reached[2]) == 1


async def test_the_feed_and_a_catalog_can_still_read_while_the_search_pool_is_stuck_on_a_lock(
    wired_client: AsyncClient, migrated_database: str
) -> None:
    """A search blocked on `catalog_search` holds a search-pool connection. The feed and one
    catalog never wait for it, for two reasons that this test names: they run on another pool, and
    they read `catalogs`, `links` and the child tables, which are not the table that is locked
    (the search's own statement is the only reader of `catalog_search`)."""
    async with (
        committed_catalogs(migrated_database, [probe_document()], recommended=True) as (
            catalog_id,
        ),
        table_locked(migrated_database),
    ):
        blocked = asyncio.create_task(wired_client.get("/search", params={"query": "paris"}))
        await asyncio.sleep(0.2)  # the search is now waiting on the lock
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            feed = await wired_client.get("/")
            single = await wired_client.get(f"/catalogs/{catalog_id}")
        assert not blocked.done()
        response = await asyncio.wait_for(blocked, HANG_GUARD_SECONDS)

    assert feed.status_code == 200
    assert [c["metadata"]["title"] for c in feed.json()["catalogs"]] == [PROBE_TITLE]
    assert single.status_code == 200
    assert response.status_code == 503
