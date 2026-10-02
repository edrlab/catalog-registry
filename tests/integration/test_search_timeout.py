"""The statement timeout and the read-only transaction (ADR-058), against real Postgres.

The slow statement is a real one: a second connection holds `ACCESS EXCLUSIVE` on
`catalog_search`, so the search blocks on the lock, and `statement_timeout` covers lock waits.
These tests use their own engines on the migrated database, not the rollback fixtures, because
the lock has to be taken by a different connection than the one searching. Nothing is
committed: the locking transaction is rolled back in a `finally`, and the engines disposed.
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from registry.core.errors import SearchTimeoutError
from registry.db.session import (
    build_read_session_factory,
    build_session_factory,
    read_only_transaction,
)
from registry.domain.search_query import parse_search_query
from registry.repositories.search_repository import CatalogSearchRepository
from tests.search_helpers import HANG_GUARD_SECONDS, table_locked

pytestmark = pytest.mark.integration

QUERY = parse_search_query("paris")


async def search_paris(session: AsyncSession) -> object:
    return await CatalogSearchRepository(session).search_catalogs(QUERY, limit=50, offset=0)


@pytest.fixture
async def one_connection_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """A pool of exactly one connection, so "the next checkout" is provably the same backend."""
    engine = create_async_engine(migrated_database, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


async def test_a_blocked_search_raises_search_timeout_error(
    migrated_database: str, one_connection_engine: AsyncEngine
) -> None:
    factory = build_session_factory(one_connection_engine)

    async with table_locked(migrated_database):
        started = time.monotonic()
        # A missing timeout would block until the lock is released: fail, do not hang.
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            with pytest.raises(SearchTimeoutError, match="took too long"):
                async with read_only_transaction(factory, statement_timeout_ms=150) as session:
                    await search_paris(session)
        elapsed = time.monotonic() - started

    # Cancelled by the timeout, not by luck: not instantly, and nowhere near forever.
    assert 0.1 < elapsed < 3


async def test_the_translated_error_keeps_the_database_error_as_its_cause(
    migrated_database: str, one_connection_engine: AsyncEngine
) -> None:
    factory = build_session_factory(one_connection_engine)

    async with table_locked(migrated_database):
        # A missing timeout would block until the lock is released: fail, do not hang.
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            with pytest.raises(SearchTimeoutError) as raised:
                async with read_only_transaction(factory, statement_timeout_ms=100) as session:
                    await search_paris(session)

    assert isinstance(raised.value.__cause__, DBAPIError)
    assert getattr(raised.value.__cause__.orig, "sqlstate", None) == "57014"
    assert (raised.value.status_code, raised.value.title) == (503, "Service Unavailable")


async def test_pg_sleep_past_the_timeout_is_cancelled_with_the_query_canceled_state(
    one_connection_engine: AsyncEngine,
) -> None:
    """The plan's own probe. Translation happens in the repository, so this is the raw error."""
    factory = build_session_factory(one_connection_engine)

    with pytest.raises(DBAPIError) as raised:
        async with read_only_transaction(factory, statement_timeout_ms=100) as session:
            await session.execute(text("SELECT pg_sleep(0.4)"))

    assert getattr(raised.value.orig, "sqlstate", None) == "57014"


async def test_a_statement_inside_the_timeout_completes(
    one_connection_engine: AsyncEngine,
) -> None:
    factory = build_session_factory(one_connection_engine)

    async with read_only_transaction(factory, statement_timeout_ms=1000) as session:
        assert (await session.scalar(text("SELECT pg_sleep(0.05) IS NOT NULL"))) is True


async def test_the_timeout_does_not_leak_to_the_pooled_connection(
    migrated_database: str, one_connection_engine: AsyncEngine
) -> None:
    factory = build_session_factory(one_connection_engine)
    async with one_connection_engine.connect() as connection:
        default = await connection.scalar(text("SHOW statement_timeout"))
        backend = await connection.scalar(text("SELECT pg_backend_pid()"))

    async with table_locked(migrated_database):
        # A missing timeout would block until the lock is released: fail, do not hang.
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            with pytest.raises(SearchTimeoutError):
                async with read_only_transaction(factory, statement_timeout_ms=120) as session:
                    assert await session.scalar(text("SHOW statement_timeout")) == "120ms"
                    assert await session.scalar(text("SELECT pg_backend_pid()")) == backend
                    await search_paris(session)

    async with one_connection_engine.connect() as connection:
        assert await connection.scalar(text("SELECT pg_backend_pid()")) == backend
        assert await connection.scalar(text("SHOW statement_timeout")) == default
        assert await connection.scalar(text("SHOW transaction_read_only")) == "off"


async def test_the_setting_does_not_leak_after_a_search_that_succeeds_either(
    one_connection_engine: AsyncEngine,
) -> None:
    factory = build_session_factory(one_connection_engine)
    async with one_connection_engine.connect() as connection:
        default = await connection.scalar(text("SHOW statement_timeout"))

    async with read_only_transaction(factory, statement_timeout_ms=777) as session:
        assert await session.scalar(text("SHOW statement_timeout")) == "777ms"

    async with one_connection_engine.connect() as connection:
        assert await connection.scalar(text("SHOW statement_timeout")) == default


async def test_the_transaction_is_read_only(one_connection_engine: AsyncEngine) -> None:
    factory = build_session_factory(one_connection_engine)

    async with read_only_transaction(factory, statement_timeout_ms=1000) as session:
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        with pytest.raises(DBAPIError) as raised:
            await session.execute(text("DELETE FROM catalog_search"))

    assert getattr(raised.value.orig, "sqlstate", None) == "25006"  # read_only_sql_transaction


async def test_an_insert_inside_it_fails_too(one_connection_engine: AsyncEngine) -> None:
    factory = build_session_factory(one_connection_engine)

    async with read_only_transaction(factory, statement_timeout_ms=1000) as session:
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await session.execute(
                text(
                    "INSERT INTO country_languages (country_code, language_tag) VALUES ('FR', 'x')"
                )
            )


async def test_the_read_factorys_autocommit_session_ignores_the_timeout(
    one_connection_engine: AsyncEngine,
) -> None:
    """Why `read_only_transaction` exists: the failure it replaces. Under AUTOCOMMIT,
    `set_config(..., true)` is local to a transaction that ends with that statement, so the
    sleep below runs to completion although the "timeout" is 100 ms (ADR-058, tested on 1 Oct)."""
    factory = build_read_session_factory(one_connection_engine)

    async with factory() as session:
        await session.execute(text("SELECT set_config('statement_timeout', '100', true)"))
        assert await session.scalar(text("SELECT pg_sleep(0.4) IS NOT NULL")) is True
        assert await session.scalar(text("SHOW statement_timeout")) != "100ms"
