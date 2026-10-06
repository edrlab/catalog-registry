"""The read pool of the feed and one catalog: its limits, its timeout and its one retry (ADR-062).

Built for real (`build_read_engine(settings, READ_CONNECTION_SETTINGS)`), against the migrated
database, and read back with `SHOW` on live connections. The slow statement is a real one: another
connection holds `ACCESS EXCLUSIVE` on `catalogs`, so the feed blocks on the lock, and
`statement_timeout` covers lock waits. A lock on `catalogs` also blocks search, so no test here
searches while it is held. Nothing is committed except the rows of the retry tests, which are
deleted in a `finally`.
"""

import asyncio
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from registry.core.config import Settings
from registry.core.errors import ReadTimeoutError, SearchTimeoutError
from registry.db.session import (
    build_read_engine,
    build_session_factory,
    create_database_engine,
)
from registry.repositories.catalog_repository import (
    READ_CONNECTION_SETTINGS,
    READ_STATEMENT_TIMEOUT_MS,
    CatalogRepository,
)
from registry.repositories.catalog_rows import fetch_rows
from registry.repositories.search_repository import (
    SEARCH_CONNECTION_SETTINGS,
    WORD_SIMILARITY_THRESHOLD,
)
from tests.conftest import SEED_CATALOG_COUNT, SEED_FILE
from tests.read_helpers import (
    StubSession,
    checked_out,
    committed_seed,
    link_document,
    locked,
    read_engine_with,
    terminate_backends,
)
from tests.search_helpers import HANG_GUARD_SECONDS, committed_catalogs, table_locked

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated_database")]

SHORT = "150"  # milliseconds: long enough to be a timeout, short enough for a quick suite
INSERT = text("INSERT INTO subdivisions (code, country_alpha2) VALUES ('ZZ-99', 'FR')")


async def show(engine: AsyncEngine, *names: str) -> dict[str, str | None]:
    """Server settings as live connections of *engine* report them."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT public.word_similarity('a', 'a')"))  # loads pg_trgm
        return {name: await connection.scalar(text(f"SHOW {name}")) for name in names}


# --- D. the settings each pool carries ----------------------------------------------------------


def test_the_production_constants() -> None:
    assert READ_STATEMENT_TIMEOUT_MS == 10_000
    assert READ_CONNECTION_SETTINGS == {
        "statement_timeout": "10000",
        "default_transaction_read_only": "on",
    }
    # Different pools, different limits: the feed's allowance is not search's.
    assert SEARCH_CONNECTION_SETTINGS["statement_timeout"] == "1000"
    assert "pg_trgm.word_similarity_threshold" not in READ_CONNECTION_SETTINGS


async def test_the_read_pool_carries_a_ten_second_timeout_and_read_only_mode(
    settings: Settings,
) -> None:
    engine = build_read_engine(settings, READ_CONNECTION_SETTINGS)
    try:
        seen = await show(
            engine, "statement_timeout", "default_transaction_read_only", "transaction_read_only"
        )
    finally:
        await engine.dispose()

    assert seen == {
        "statement_timeout": "10s",
        "default_transaction_read_only": "on",
        "transaction_read_only": "on",
    }


async def test_the_read_pool_does_not_carry_the_search_typo_threshold(settings: Settings) -> None:
    read = build_read_engine(settings, READ_CONNECTION_SETTINGS)
    search = build_read_engine(settings, SEARCH_CONNECTION_SETTINGS)
    main = create_database_engine(settings)
    try:
        threshold = "pg_trgm.word_similarity_threshold"
        seen_read = await show(read, threshold)
        seen_search = await show(search, threshold)
        seen_main = await show(main, threshold)
    finally:
        for engine in (read, search, main):
            await engine.dispose()

    assert seen_search[threshold] == str(WORD_SIMILARITY_THRESHOLD)
    assert seen_read[threshold] == seen_main[threshold] != seen_search[threshold]


async def test_the_search_pool_still_has_its_own_limits(settings: Settings) -> None:
    engine = build_read_engine(settings, SEARCH_CONNECTION_SETTINGS)
    try:
        seen = await show(
            engine,
            "statement_timeout",
            "default_transaction_read_only",
            "pg_trgm.word_similarity_threshold",
        )
    finally:
        await engine.dispose()

    assert seen == {
        "statement_timeout": "1s",
        "default_transaction_read_only": "on",
        "pg_trgm.word_similarity_threshold": str(WORD_SIMILARITY_THRESHOLD),
    }


async def test_the_main_engine_has_no_timeout_and_is_not_read_only(settings: Settings) -> None:
    engine = create_database_engine(settings)
    try:
        seen = await show(engine, "statement_timeout", "default_transaction_read_only")
    finally:
        await engine.dispose()

    assert seen == {"statement_timeout": "0", "default_transaction_read_only": "off"}


async def test_a_write_through_the_read_pool_fails_with_read_only_sql_transaction(
    settings: Settings,
) -> None:
    async with read_engine_with(settings) as engine, engine.connect() as connection:
        with pytest.raises(DBAPIError) as raised:
            await connection.execute(INSERT)

    assert getattr(raised.value.orig, "sqlstate", None) == "25006"  # read_only_sql_transaction


async def test_the_main_engine_can_write_the_same_statement(settings: Settings) -> None:
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            result = await connection.execute(INSERT)  # rolled back below: nothing is committed
            await transaction.rollback()
    finally:
        await engine.dispose()

    assert result.rowcount == 1


async def test_the_read_pool_cannot_delete_or_update_either(
    settings: Settings, migrated_database: str
) -> None:
    async with (
        committed_catalogs(migrated_database, [link_document("Untouchable")], recommended=True),
        read_engine_with(settings) as engine,
        engine.connect() as connection,
    ):
        for statement in ("DELETE FROM catalogs", "UPDATE catalogs SET title = 'x'"):
            with pytest.raises(DBAPIError) as raised:
                await connection.execute(text(statement))
            assert getattr(raised.value.orig, "sqlstate", None) == "25006"
        assert await connection.scalar(text("SELECT count(*) FROM catalogs")) == 1


async def test_the_read_pool_still_reads_what_it_is_for(
    settings: Settings, migrated_database: str
) -> None:
    async with (
        committed_seed(migrated_database, (SEED_FILE, True)),
        read_engine_with(settings) as engine,
        build_session_factory(engine)() as session,
    ):
        feed = await CatalogRepository(session).fetch_recommended_catalogs()

    assert len(feed) == SEED_CATALOG_COUNT


# --- D. the timeout -----------------------------------------------------------------------------


async def test_a_blocked_feed_raises_read_timeout_error_not_search_timeout_error(
    settings: Settings, migrated_database: str
) -> None:
    async with read_engine_with(settings, statement_timeout=SHORT) as engine:
        factory = build_session_factory(engine)
        async with locked(migrated_database, "catalogs"):
            started = time.monotonic()
            # A missing timeout would block until the lock is released: fail, do not hang.
            async with asyncio.timeout(HANG_GUARD_SECONDS):
                with pytest.raises(ReadTimeoutError, match="took too long") as raised:
                    async with factory() as session:
                        await CatalogRepository(session).fetch_recommended_catalogs()
            elapsed = time.monotonic() - started

    assert type(raised.value) is ReadTimeoutError
    assert not isinstance(raised.value, SearchTimeoutError)
    assert isinstance(raised.value.__cause__, DBAPIError)
    assert getattr(raised.value.__cause__.orig, "sqlstate", None) == "57014"
    assert (raised.value.status_code, raised.value.title) == (503, "Service Unavailable")
    # Cancelled by the timeout, not by luck, and not retried: one 150 ms wait, not two.
    assert 0.1 < elapsed < 0.3 * 2 + 0.1


async def test_a_blocked_catalog_read_raises_read_timeout_error_too(
    settings: Settings, migrated_database: str
) -> None:
    async with read_engine_with(settings, statement_timeout=SHORT) as engine:
        factory = build_session_factory(engine)
        async with locked(migrated_database, "catalogs"):
            async with asyncio.timeout(HANG_GUARD_SECONDS):
                with pytest.raises(ReadTimeoutError):
                    async with factory() as session:
                        await CatalogRepository(session).fetch_catalog_by_id(uuid.uuid4())


async def test_the_feed_reads_again_when_the_lock_is_gone(
    settings: Settings, migrated_database: str
) -> None:
    async with read_engine_with(settings, statement_timeout=SHORT) as engine:
        factory = build_session_factory(engine)
        async with locked(migrated_database, "catalogs"):
            async with asyncio.timeout(HANG_GUARD_SECONDS):
                with pytest.raises(ReadTimeoutError):
                    async with factory() as session:
                        await CatalogRepository(session).fetch_recommended_catalogs()

        async with factory() as session:
            assert await CatalogRepository(session).fetch_recommended_catalogs() == []
        assert checked_out(engine) == 0  # the cancelled connection came back


async def test_a_timeout_on_one_pool_does_not_reach_the_other_tables_read(
    settings: Settings, migrated_database: str
) -> None:
    """Different tables locked, different pools: a search stuck on `catalog_search` does not stop
    the feed (which does not read that table)."""
    async with read_engine_with(settings) as engine:
        factory = build_session_factory(engine)
        async with table_locked(migrated_database):
            async with asyncio.timeout(HANG_GUARD_SECONDS):
                async with factory() as session:
                    assert await CatalogRepository(session).fetch_recommended_catalogs() == []


def test_a_search_timeout_is_a_read_timeout() -> None:
    assert issubclass(SearchTimeoutError, ReadTimeoutError)
    assert SearchTimeoutError.status_code == ReadTimeoutError.status_code == 503


async def test_a_blocked_search_still_raises_search_timeout_error(
    settings: Settings, migrated_database: str
) -> None:
    from registry.domain.search_query import parse_search_query  # noqa: PLC0415
    from registry.repositories.search_repository import CatalogSearchRepository  # noqa: PLC0415
    from tests.search_helpers import search_engine_with  # noqa: PLC0415

    async with search_engine_with(settings, statement_timeout=SHORT) as engine:
        factory = build_session_factory(engine)
        async with table_locked(migrated_database):
            async with asyncio.timeout(HANG_GUARD_SECONDS):
                with pytest.raises(SearchTimeoutError) as raised:
                    async with factory() as session:
                        await CatalogSearchRepository(session).search_catalogs(
                            parse_search_query("paris"), limit=50, offset=0
                        )

    assert isinstance(raised.value, ReadTimeoutError)


# --- E. the one retry ---------------------------------------------------------------------------


def documents() -> list[dict[str, Any]]:
    return [link_document(f"retry shelf {n}") for n in range(3)]


async def test_a_feed_read_on_a_terminated_connection_succeeds_through_the_one_retry(
    settings: Settings, migrated_database: str
) -> None:
    name = f"retry-{uuid.uuid4().hex[:8]}"
    async with (
        committed_catalogs(migrated_database, documents(), recommended=True),
        read_engine_with(settings, application_name=name) as engine,
    ):
        factory = build_session_factory(engine)
        async with factory() as session:
            first = [c.title for c in await CatalogRepository(session).fetch_recommended_catalogs()]
        killed = await terminate_backends(migrated_database, name)

        async with factory() as session:
            second = [
                c.title for c in await CatalogRepository(session).fetch_recommended_catalogs()
            ]

    assert killed == 1  # one pooled connection, now dead
    assert len(first) == 3
    assert second == first


async def test_a_catalog_read_on_a_terminated_connection_succeeds_through_the_one_retry(
    settings: Settings, migrated_database: str
) -> None:
    name = f"retry-{uuid.uuid4().hex[:8]}"
    async with (
        committed_catalogs(migrated_database, documents(), recommended=True) as ids,
        read_engine_with(settings, application_name=name) as engine,
    ):
        factory = build_session_factory(engine)
        async with factory() as session:
            before = await CatalogRepository(session).fetch_catalog_by_id(ids[0])
        killed = await terminate_backends(migrated_database, name)
        async with factory() as session:
            after = await CatalogRepository(session).fetch_catalog_by_id(ids[0])

    assert killed == 1
    assert before is not None and after is not None
    assert after.title == before.title


async def test_the_retry_ran_on_a_new_backend_and_the_pool_stays_usable(
    settings: Settings, migrated_database: str
) -> None:
    name = f"retry-{uuid.uuid4().hex[:8]}"
    async with (
        committed_catalogs(migrated_database, documents(), recommended=True),
        read_engine_with(settings, application_name=name) as engine,
    ):
        factory = build_session_factory(engine)
        async with engine.connect() as connection:
            before = await connection.scalar(text("SELECT pg_backend_pid()"))
        await terminate_backends(migrated_database, name)

        results = []
        for _ in range(3):
            async with factory() as session:
                results.append(len(await CatalogRepository(session).fetch_recommended_catalogs()))
        async with engine.connect() as connection:
            after = await connection.scalar(text("SELECT pg_backend_pid()"))

    assert results == [3, 3, 3]
    assert after != before
    assert checked_out(engine) == 0


class SqlstateError(Exception):
    """What asyncpg puts in `DBAPIError.orig`: an exception that carries a SQLSTATE."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def dropped() -> DBAPIError:
    return DBAPIError("SELECT", {}, ConnectionResetError("gone"), connection_invalidated=True)


async def test_the_retry_happens_at_most_once_for_the_feed_and_for_a_catalog() -> None:
    for by_id in (False, True):
        stub = StubSession(dropped(), failures=99)
        repository = CatalogRepository(stub)  # type: ignore[arg-type]

        with pytest.raises(DBAPIError) as raised:
            if by_id:
                await repository.fetch_catalog_by_id(uuid.uuid4())
            else:
                await repository.fetch_recommended_catalogs()

        assert raised.value.connection_invalidated
        assert (stub.executes, stub.rollbacks) == (2, 1)


async def test_a_second_attempt_that_works_is_returned_after_one_rollback() -> None:
    stub = StubSession(dropped(), failures=1, rows=[])

    assert await CatalogRepository(stub).fetch_recommended_catalogs() == []  # type: ignore[arg-type]
    assert (stub.executes, stub.rollbacks) == (2, 1)

    stub = StubSession(dropped(), failures=1, rows=[])
    assert await CatalogRepository(stub).fetch_catalog_by_id(uuid.uuid4()) is None  # type: ignore[arg-type]
    assert (stub.executes, stub.rollbacks) == (2, 1)


async def test_an_error_that_is_not_a_dropped_connection_is_raised_at_the_first_attempt() -> None:
    error = DBAPIError("SELECT", {}, RuntimeError("syntax"), connection_invalidated=False)
    stub = StubSession(error, failures=99)

    with pytest.raises(DBAPIError):
        await CatalogRepository(stub).fetch_recommended_catalogs()  # type: ignore[arg-type]

    assert (stub.executes, stub.rollbacks) == (1, 0)


async def test_a_database_error_is_raised_unchanged_and_not_retried(settings: Settings) -> None:
    """A real, non-connection error: division by zero. One statement, then it propagates."""
    from tests.read_helpers import Statements  # noqa: PLC0415

    async with read_engine_with(settings) as engine:
        counter = Statements(engine)
        async with build_session_factory(engine)() as session:
            with pytest.raises(DBAPIError) as raised:
                await fetch_rows(session, text("SELECT 1 / 0"), {})

    assert not raised.value.connection_invalidated
    assert getattr(raised.value.orig, "sqlstate", None) == "22012"
    assert len(counter.statements) == 1


async def test_a_timeout_is_not_retried_and_raises_the_error_it_was_given() -> None:
    class MineError(ReadTimeoutError):
        pass

    error = DBAPIError("SELECT", {}, SqlstateError("57014"), connection_invalidated=False)
    stub = StubSession(error, failures=99)
    mine = MineError("my own words")

    with pytest.raises(MineError, match="my own words"):
        await fetch_rows(stub, text("SELECT 1"), {}, timeout=mine)  # type: ignore[arg-type]

    assert (stub.executes, stub.rollbacks) == (1, 0)


async def test_a_timeout_without_a_given_error_is_a_plain_read_timeout() -> None:
    error = DBAPIError("SELECT", {}, SqlstateError("57014"), connection_invalidated=False)
    stub = StubSession(error, failures=99)

    with pytest.raises(ReadTimeoutError) as raised:
        await fetch_rows(stub, text("SELECT 1"), {})  # type: ignore[arg-type]

    assert type(raised.value) is ReadTimeoutError
    assert raised.value.__cause__ is error
