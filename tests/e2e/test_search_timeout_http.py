"""The search timeout through the real app: its own engine, its own transactional factory.

Unlike the rest of the e2e suite this does not rebind the app to the rollback fixture, because
the timeout has to be proven on the wiring `main.py` builds. A second connection holds
`ACCESS EXCLUSIVE` on `catalog_search` so the search blocks; the lock is rolled back in a
`finally` and nothing is committed.
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from registry.core.config import Settings
from registry.core.constants import PROBLEM_JSON_MEDIA_TYPE
from registry.main import SEARCH_STATEMENT_TIMEOUT_MS, create_app
from tests.search_helpers import HANG_GUARD_SECONDS, table_locked

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


async def test_the_search_session_is_a_read_only_transaction_with_the_one_second_timeout(
    wired_app: FastAPI,
) -> None:
    """Not the read path's AUTOCOMMIT session (ADR-058): transactional, read-only, 1 s."""
    async with wired_app.state.open_catalog_searcher() as searcher:
        session = searcher._session  # the searcher's own session, not a copy
        assert session.in_transaction()
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        assert await session.scalar(text("SHOW statement_timeout")) == "1s"


async def test_the_search_factory_is_not_the_read_factory(wired_app: FastAPI) -> None:
    assert wired_app.state.search_session_factory is not wired_app.state.session_factory
    read_engine = wired_app.state.session_factory.kw["bind"]
    search_engine = wired_app.state.search_session_factory.kw["bind"]
    assert read_engine.get_execution_options().get("isolation_level") == "AUTOCOMMIT"
    assert search_engine.get_execution_options().get("isolation_level") != "AUTOCOMMIT"


async def test_a_timeout_does_not_poison_the_next_request_on_the_same_app(
    wired_client: AsyncClient, migrated_database: str
) -> None:
    async with table_locked(migrated_database):
        await asyncio.wait_for(
            wired_client.get("/search", params={"query": "paris"}), HANG_GUARD_SECONDS
        )

    # The feed uses the AUTOCOMMIT read factory on the same engine; it must be unaffected.
    assert (await wired_client.get("/")).status_code == 200
