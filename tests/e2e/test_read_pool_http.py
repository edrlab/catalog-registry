"""The feed and `GET /catalogs/{id}` through the real app and its read pool (ADR-062).

Not rebound to the rollback fixture: the timeout, the read-only mode and the pool's behaviour under
load have to be proven on the wiring `main.py` builds (`create_app` and its lifespan), against the
migrated database. Rows are really committed and deleted in a `finally`.

The production timeout is 10 s. A test that waited that long would be a slow test, so the 503 tests
swap the app's read session factory for one on an engine built the same way
(`build_read_engine(settings, {**READ_CONNECTION_SETTINGS, "statement_timeout": "150"})`); the
production value itself is read back from the app's own engine.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from registry.core.config import Settings
from registry.core.constants import PROBLEM_JSON_MEDIA_TYPE
from registry.db.session import build_session_factory
from registry.main import create_app
from tests.read_helpers import link_document, locked, read_engine_with
from tests.search_helpers import (
    HANG_GUARD_SECONDS,
    assert_valid_catalog,
    assert_valid_feed,
    committed_catalogs,
)

pytestmark = pytest.mark.e2e

CONCURRENT = 30
CATALOGS = 12


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


def documents() -> list[dict[str, Any]]:
    return [
        link_document(
            f"{'okapi' if n % 2 else 'pangolin'} shelf {n:02d}",
            supportedLanguages=["fr", "en"] if n % 3 else ["de"],
            rel_extra=[{"href": f"https://load.example/{n}/icon", "rel": "icon"}],
        )
        for n in range(CATALOGS)
    ]


# --- D. what the app's own read pool carries -----------------------------------------------------


async def test_the_apps_read_pool_has_the_production_limits(wired_app: FastAPI) -> None:
    async with wired_app.state.open_catalog_reader() as reader:
        session = reader._session  # the reader's own session, not a copy
        assert await session.scalar(text("SHOW statement_timeout")) == "10s"
        assert await session.scalar(text("SHOW default_transaction_read_only")) == "on"
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"


# --- D. 503 over HTTP -----------------------------------------------------------------------------


@pytest.fixture
async def short_timeout(wired_app: FastAPI, settings: Settings) -> AsyncIterator[None]:
    """The app's feed/catalog factory, on a pool whose timeout is 150 ms."""
    async with read_engine_with(settings, statement_timeout="150") as engine:
        wired_app.state.session_factory = build_session_factory(engine)
        yield


def assert_problem_503(response: Any) -> None:
    assert response.status_code == 503
    assert response.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    body = response.json()
    assert body["status"] == 503
    assert body["title"] == "Service Unavailable"
    assert "too long" in body["detail"]
    assert body["type"] == "about:blank"


@pytest.mark.usefixtures("short_timeout")
async def test_a_blocked_feed_answers_503_problem_json(
    wired_client: AsyncClient, migrated_database: str
) -> None:
    async with locked(migrated_database, "catalogs"):
        response = await asyncio.wait_for(wired_client.get("/"), HANG_GUARD_SECONDS)

    assert_problem_503(response)


@pytest.mark.usefixtures("short_timeout")
async def test_a_blocked_catalog_answers_503_problem_json(
    wired_client: AsyncClient, migrated_database: str
) -> None:
    async with locked(migrated_database, "catalogs"):
        response = await asyncio.wait_for(
            wired_client.get(f"/catalogs/{uuid.uuid4()}"), HANG_GUARD_SECONDS
        )

    assert_problem_503(response)


@pytest.mark.usefixtures("short_timeout")
async def test_the_feed_and_the_catalog_recover_when_the_lock_is_gone(
    wired_client: AsyncClient, migrated_database: str
) -> None:
    async with committed_catalogs(
        migrated_database, [link_document("Recovery probe")], recommended=True
    ) as (catalog_id,):
        async with locked(migrated_database, "catalogs"):
            blocked = await asyncio.wait_for(wired_client.get("/"), HANG_GUARD_SECONDS)
            assert blocked.status_code == 503

        feed = await wired_client.get("/")
        single = await wired_client.get(f"/catalogs/{catalog_id}")

    assert feed.status_code == 200
    assert_valid_feed(feed.json())
    assert single.status_code == 200
    assert_valid_catalog(single.json())


@pytest.mark.usefixtures("short_timeout")
async def test_a_lock_on_the_search_table_does_not_affect_the_feed_or_a_catalog(
    wired_client: AsyncClient, migrated_database: str
) -> None:
    """The feed and one catalog do not read `catalog_search`."""
    from tests.search_helpers import table_locked  # noqa: PLC0415

    async with (
        committed_catalogs(migrated_database, [link_document("Probe")], recommended=True) as (
            catalog_id,
        ),
        table_locked(migrated_database),
    ):
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            feed = await wired_client.get("/")
            single = await wired_client.get(f"/catalogs/{catalog_id}")

    assert (feed.status_code, single.status_code) == (200, 200)


# --- unchanged behaviour of the endpoints ---------------------------------------------------------


async def test_an_unknown_catalog_is_404_and_a_malformed_id_is_422(
    wired_client: AsyncClient,
) -> None:
    missing = await wired_client.get(f"/catalogs/{uuid.uuid4()}")
    malformed = await wired_client.get("/catalogs/not-a-uuid")

    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert malformed.status_code == 422


# --- F. concurrency -------------------------------------------------------------------------------


async def test_thirty_concurrent_feeds_return_the_same_body_and_leak_no_connection(
    wired_app: FastAPI, migrated_database: str
) -> None:
    async with (
        committed_catalogs(migrated_database, documents(), recommended=True),
        AsyncClient(transport=ASGITransport(app=wired_app), base_url="http://testserver") as client,
    ):
        alone = (await client.get("/")).json()
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            responses = await asyncio.gather(*(client.get("/") for _ in range(CONCURRENT)))

    assert alone["metadata"]["numberOfItems"] == CATALOGS
    assert_valid_feed(alone)
    for response in responses:
        assert response.status_code == 200
        assert response.json() == alone
    assert len({r.content for r in responses}) == 1
    pool = wired_app.state.read_engine.sync_engine.pool
    assert pool.checkedout() == 0
    assert pool.checkedin() >= 1


async def test_thirty_concurrent_catalog_reads_return_the_right_catalog_and_leak_nothing(
    wired_app: FastAPI, migrated_database: str
) -> None:
    async with (
        committed_catalogs(migrated_database, documents(), recommended=True) as ids,
        AsyncClient(transport=ASGITransport(app=wired_app), base_url="http://testserver") as client,
    ):
        alone = {i: (await client.get(f"/catalogs/{i}")).json() for i in ids}
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            responses = await asyncio.gather(
                *(client.get(f"/catalogs/{ids[n % len(ids)]}") for n in range(CONCURRENT))
            )

    for n, response in enumerate(responses):
        catalog_id = ids[n % len(ids)]
        assert response.status_code == 200
        assert response.json() == alone[catalog_id]
        assert response.json()["metadata"]["identifier"] == f"urn:uuid:{catalog_id}"
        assert_valid_catalog(response.json())
    pool = wired_app.state.read_engine.sync_engine.pool
    assert pool.checkedout() == 0
    assert pool.checkedin() >= 1


async def test_feeds_catalogs_and_searches_at_once_do_not_disturb_each_other(
    wired_app: FastAPI, migrated_database: str
) -> None:
    async with (
        committed_catalogs(migrated_database, documents(), recommended=True) as ids,
        AsyncClient(transport=ASGITransport(app=wired_app), base_url="http://testserver") as client,
    ):
        feed = (await client.get("/")).json()
        one = (await client.get(f"/catalogs/{ids[0]}")).json()
        okapi = (await client.get("/search", params={"query": "okapi"})).json()
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            responses = await asyncio.gather(
                *(
                    client.get(
                        ["/", f"/catalogs/{ids[0]}", "/search?query=okapi"][n % 3],
                    )
                    for n in range(CONCURRENT)
                )
            )

    expected = [feed, one, okapi]
    for n, response in enumerate(responses):
        assert response.status_code == 200
        assert response.json() == expected[n % 3]
    for engine in (wired_app.state.read_engine, wired_app.state.search_engine):
        assert engine.sync_engine.pool.checkedout() == 0


async def test_the_body_is_the_same_text_every_time(
    wired_client: AsyncClient, migrated_database: str
) -> None:
    async with committed_catalogs(migrated_database, documents(), recommended=True):
        first = await wired_client.get("/")
        second = await wired_client.get("/")

    assert first.content == second.content
    assert json.loads(first.content)["metadata"]["numberOfItems"] == CATALOGS
