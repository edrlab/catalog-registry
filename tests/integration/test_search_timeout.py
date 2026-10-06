"""The search pool's limits, enforced by the connection itself (ADR-058 amended, ADR-060).

Search runs on `build_read_engine`: AUTOCOMMIT, with `statement_timeout`, read-only mode and the
typo threshold sent once per connection in the startup packet. These tests build that engine for
real, against the migrated database, and read the settings back with `SHOW` on live connections.

The slow statement is a real one: a second connection holds `ACCESS EXCLUSIVE` on
`catalog_search`, so the search blocks on the lock, and `statement_timeout` covers lock waits. They
use their own engines, not the rollback fixtures, because the lock has to be taken by a different
connection than the one searching. Nothing is committed: the locking transaction is rolled back in
a `finally`, and the engines disposed.
"""

import asyncio
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.core.config import Settings
from registry.core.errors import SearchTimeoutError
from registry.db.session import build_read_engine, build_session_factory, create_database_engine
from registry.domain.search_query import parse_search_query
from registry.repositories.protocols import SearchPage
from registry.repositories.search_repository import (
    SEARCH_CONNECTION_SETTINGS,
    CatalogSearchRepository,
)
from tests.search_helpers import HANG_GUARD_SECONDS, search_engine_with, table_locked

pytestmark = pytest.mark.integration

QUERY = parse_search_query("paris")
SHORT = "150"  # milliseconds: long enough to be a timeout, short enough for a quick suite


async def search_paris(factory: async_sessionmaker[AsyncSession]) -> SearchPage:
    async with factory() as session:
        return await CatalogSearchRepository(session).search_catalogs(QUERY, limit=50, offset=0)


async def test_a_blocked_search_raises_search_timeout_error(
    settings: Settings, migrated_database: str
) -> None:
    async with search_engine_with(settings, statement_timeout=SHORT) as engine:
        factory = build_session_factory(engine)
        async with table_locked(migrated_database):
            started = time.monotonic()
            # A missing timeout would block until the lock is released: fail, do not hang.
            async with asyncio.timeout(HANG_GUARD_SECONDS):
                with pytest.raises(SearchTimeoutError, match="took too long"):
                    await search_paris(factory)
            elapsed = time.monotonic() - started

    # Cancelled by the timeout, not by luck: not instantly, and nowhere near forever.
    assert 0.1 < elapsed < 3


async def test_the_translated_error_keeps_the_database_error_as_its_cause(
    settings: Settings, migrated_database: str
) -> None:
    async with search_engine_with(settings, statement_timeout=SHORT) as engine:
        factory = build_session_factory(engine)
        async with table_locked(migrated_database):
            async with asyncio.timeout(HANG_GUARD_SECONDS):
                with pytest.raises(SearchTimeoutError) as raised:
                    await search_paris(factory)

    assert isinstance(raised.value.__cause__, DBAPIError)
    assert getattr(raised.value.__cause__.orig, "sqlstate", None) == "57014"
    assert (raised.value.status_code, raised.value.title) == (503, "Service Unavailable")


async def test_a_timeout_is_not_retried(settings: Settings, migrated_database: str) -> None:
    """A slow search must cost one timeout, not two: only a dropped connection is retried."""
    async with search_engine_with(settings, statement_timeout=SHORT) as engine:
        factory = build_session_factory(engine)
        async with table_locked(migrated_database):
            started = time.monotonic()
            async with asyncio.timeout(HANG_GUARD_SECONDS):
                with pytest.raises(SearchTimeoutError):
                    await search_paris(factory)
            elapsed = time.monotonic() - started

    assert elapsed < 0.3 * 2 + 0.1  # one 150 ms wait; a retry would take about 300 ms


async def test_pg_sleep_past_the_timeout_is_cancelled_with_the_query_canceled_state(
    settings: Settings,
) -> None:
    """Translation happens in the repository, so this is the raw error from the connection."""
    async with (
        search_engine_with(settings, statement_timeout="100") as engine,
        engine.connect() as connection,
    ):
        with pytest.raises(DBAPIError) as raised:
            await connection.execute(text("SELECT pg_sleep(0.4)"))

    assert getattr(raised.value.orig, "sqlstate", None) == "57014"


async def test_a_statement_inside_the_timeout_completes(settings: Settings) -> None:
    async with (
        search_engine_with(settings, statement_timeout="1000") as engine,
        engine.connect() as connection,
    ):
        assert await connection.scalar(text("SELECT pg_sleep(0.05) IS NOT NULL")) is True


async def test_every_search_connection_carries_the_production_settings(settings: Settings) -> None:
    engine = build_read_engine(settings, SEARCH_CONNECTION_SETTINGS)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT public.word_similarity('a', 'a')"))
            seen = {
                "statement_timeout": await connection.scalar(text("SHOW statement_timeout")),
                "default_transaction_read_only": await connection.scalar(
                    text("SHOW default_transaction_read_only")
                ),
                "pg_trgm.word_similarity_threshold": await connection.scalar(
                    text("SHOW pg_trgm.word_similarity_threshold")
                ),
            }
    finally:
        await engine.dispose()

    assert seen == {
        "statement_timeout": "1s",
        "default_transaction_read_only": "on",
        "pg_trgm.word_similarity_threshold": "0.5",
    }


async def test_the_override_is_what_the_connection_enforces(settings: Settings) -> None:
    async with (
        search_engine_with(settings, statement_timeout=SHORT) as engine,
        engine.connect() as connection,
    ):
        assert await connection.scalar(text("SHOW statement_timeout")) == "150ms"


async def test_the_timeout_and_read_only_mode_do_not_exist_on_the_main_engine(
    settings: Settings,
) -> None:
    main = create_database_engine(settings)
    try:
        async with main.connect() as connection:
            assert await connection.scalar(text("SHOW statement_timeout")) == "0"
            assert await connection.scalar(text("SHOW default_transaction_read_only")) == "off"
            assert await connection.scalar(text("SHOW transaction_read_only")) == "off"
    finally:
        await main.dispose()


async def test_the_search_pool_is_read_only_and_the_main_engine_can_write(
    settings: Settings,
) -> None:
    statements = {
        "insert": "INSERT INTO country_languages (country_code, language_tag) VALUES ('FR', 'x')",
        "delete": "DELETE FROM catalog_search",
        "update": "UPDATE catalogs SET title = title",
        "temp table": "CREATE TEMP TABLE scratch (n int)",
    }
    main = create_database_engine(settings)
    try:
        async with search_engine_with(settings) as search:
            for name, statement in statements.items():
                async with search.connect() as connection:
                    with pytest.raises(DBAPIError, match="read-only transaction") as raised:
                        await connection.execute(text(statement))
                    # read_only_sql_transaction
                    assert getattr(raised.value.orig, "sqlstate", None) == "25006", name

        async with main.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text(statements["temp table"]))
            await connection.execute(text("INSERT INTO scratch VALUES (1)"))
            assert await connection.scalar(text("SELECT count(*) FROM scratch")) == 1
            await transaction.rollback()
    finally:
        await main.dispose()


async def test_a_timed_out_connection_goes_back_clean_and_works_again(
    settings: Settings, migrated_database: str
) -> None:
    """Under AUTOCOMMIT a cancelled statement leaves no open transaction behind: the same
    backend is idle afterwards and answers the next search."""
    async with search_engine_with(settings, statement_timeout=SHORT) as engine:
        factory = build_session_factory(engine)
        async with engine.connect() as connection:
            backend = await connection.scalar(text("SELECT pg_backend_pid()"))

        async with table_locked(migrated_database):
            async with asyncio.timeout(HANG_GUARD_SECONDS):
                with pytest.raises(SearchTimeoutError):
                    await search_paris(factory)

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT pg_backend_pid()")) == backend
            state = await connection.scalar(
                text("SELECT state FROM pg_stat_activity WHERE pid = :p"), {"p": backend}
            )
            assert state == "active"  # this very statement; not "idle in transaction"
        page = await search_paris(factory)

    assert page.total >= 0


async def test_the_timeout_leaves_no_idle_in_transaction_backend(
    settings: Settings, migrated_database: str
) -> None:
    name = f"timeout-test-{uuid.uuid4().hex[:8]}"
    observer = create_async_engine(migrated_database)
    try:
        async with search_engine_with(
            settings, statement_timeout=SHORT, application_name=name
        ) as engine:
            factory = build_session_factory(engine)
            async with table_locked(migrated_database):
                async with asyncio.timeout(HANG_GUARD_SECONDS):
                    with pytest.raises(SearchTimeoutError):
                        await search_paris(factory)
            async with observer.connect() as connection:
                states = (
                    await connection.scalars(
                        text("SELECT state FROM pg_stat_activity WHERE application_name = :n"),
                        {"n": name},
                    )
                ).all()
    finally:
        await observer.dispose()

    assert states == ["idle"]


async def test_historical_set_local_under_autocommit_was_ignored(settings: Settings) -> None:
    """HISTORICAL NOTE, not a dependency of the design. ADR-058 first tried
    `SELECT set_config('statement_timeout', ..., true)` (the same as `SET LOCAL`) on an
    AUTOCOMMIT session and measured it silently ignored: `LOCAL` lasts to the end of the
    transaction, and under AUTOCOMMIT that is the end of the statement. That is why the timeout
    moved to the connection's startup settings. This documents the trap for whoever is tempted to
    put a per-request timeout back."""
    engine = create_database_engine(settings).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT set_config('statement_timeout', '100', true)"))
            assert await connection.scalar(text("SELECT pg_sleep(0.4) IS NOT NULL")) is True
            assert await connection.scalar(text("SHOW statement_timeout")) != "100ms"
    finally:
        await engine.dispose()
