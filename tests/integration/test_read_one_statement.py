"""One statement, one round trip: what the feed and a single catalog cost the connection (ADR-062).

The feed used to be six ORM statements (the catalogs and one `selectinload` per collection) inside
a transaction on the main pool. Now `fetch_recommended_catalogs` and `fetch_catalog_by_id` are one
statement each on the read pool (AUTOCOMMIT, settings on the connection), whatever the number of
catalogs.

Two counters, because each sees what the other cannot:

* `before_cursor_execute` on the read engine counts SQLAlchemy's statements.
* The loopback proxy of `tests/wire_proxy.py` counts the client's round trips (bytes sent, then
  the server's answer; TCP reads are not messages): the real measure, which also sees what the
  driver sends by itself.

Their own engines and really committed rows (deleted in a `finally`): the rollback fixtures are
invisible to a second connection, and the proxy needs real ones.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from registry.core.config import Settings
from registry.db.session import build_session_factory
from registry.domain.enums import CatalogStatus
from registry.repositories.catalog_repository import CatalogRepository
from tests.conftest import LIBRARIES_FILE, SEED_CATALOG_COUNT, SEED_FILE
from tests.read_helpers import Statements, committed_seed, link_document, read_engine_with
from tests.search_helpers import committed_catalogs
from tests.wire_proxy import WireLog, counting_proxy

pytestmark = pytest.mark.integration

#: Measured on 7 October 2026 against Postgres 18: exactly 1 round trip for a warm feed or catalog;
#: six statements in a transaction (9 round trips) was the old design.
WARM_READ_MAX_MESSAGES = 1
#: A new connection's one-off cost (startup packet, SCRAM, SQLAlchemy's dialect probes, asyncpg's
#: type introspection), paid once per pooled connection and never per request. The search's was
#: measured at 20; `test_a_cold_feed_sends_a_small_bounded_number_of_messages` reports this one.
COLD_READ_MAX_MESSAGES = 25  # round trips; measured at 20

MANY = 60


@pytest.fixture
async def seeded(migrated_database: str) -> AsyncIterator[list[uuid.UUID]]:
    """The 8 recommended catalogs and the 4 libraries, committed."""
    async with committed_seed(migrated_database, (SEED_FILE, True), (LIBRARIES_FILE, False)) as ids:
        yield ids


@pytest.fixture
async def counted(settings: Settings) -> AsyncIterator[tuple[AsyncEngine, Statements]]:
    async with read_engine_with(settings) as engine:
        yield engine, Statements(engine)


async def read_feed(engine: AsyncEngine) -> int:
    async with build_session_factory(engine)() as session:
        return len(await CatalogRepository(session).fetch_recommended_catalogs())


async def read_catalog(engine: AsyncEngine, catalog_id: uuid.UUID) -> bool:
    async with build_session_factory(engine)() as session:
        return await CatalogRepository(session).fetch_catalog_by_id(catalog_id) is not None


# --- A. statements at the driver -----------------------------------------------------------------


async def test_the_feed_is_one_statement_for_the_seeded_catalogs(
    seeded: list[uuid.UUID], counted: tuple[AsyncEngine, Statements]
) -> None:
    engine, counter = counted

    assert await read_feed(engine) == SEED_CATALOG_COUNT
    assert len(counter.statements) == 1
    assert counter.statements[0].lstrip().startswith("SELECT")
    assert counter.transaction_control == []


async def test_the_feed_is_still_one_statement_for_sixty_more_catalogs(
    migrated_database: str, counted: tuple[AsyncEngine, Statements]
) -> None:
    """The N+1 rule (R4), executable: the count does not grow with the number of catalogs."""
    engine, counter = counted
    documents = [
        link_document(
            f"Bulk {n:02d}",
            kind=["public", "school"],
            supportedLanguages=["fr", "en"],
            publicationTypes=["ebook"],
        )
        for n in range(MANY)
    ]
    async with committed_catalogs(migrated_database, documents, recommended=True):
        assert await read_feed(engine) == MANY

    assert len(counter.statements) == 1
    assert counter.transaction_control == []


async def test_one_catalog_is_one_statement(
    seeded: list[uuid.UUID], counted: tuple[AsyncEngine, Statements]
) -> None:
    engine, counter = counted

    for catalog_id in seeded:
        assert await read_catalog(engine, catalog_id)

    assert len(counter.statements) == len(seeded) == SEED_CATALOG_COUNT + 4
    assert counter.transaction_control == []


async def test_load_catalog_by_id_is_one_statement_too(
    seeded: list[uuid.UUID], counted: tuple[AsyncEngine, Statements]
) -> None:
    engine, counter = counted

    async with build_session_factory(engine)() as session:
        catalog = await CatalogRepository(session).load_catalog_by_id(seeded[0])

    assert catalog.id == seeded[0]
    assert len(counter.statements) == 1


async def test_an_unknown_id_is_one_statement_and_none(
    counted: tuple[AsyncEngine, Statements],
) -> None:
    engine, counter = counted

    assert not await read_catalog(engine, uuid.uuid4())
    assert len(counter.statements) == 1
    assert counter.transaction_control == []


async def test_an_inactive_catalog_is_one_statement_and_none(
    migrated_database: str, counted: tuple[AsyncEngine, Statements]
) -> None:
    """A suggested catalog is somebody's unreviewed submission: not public, and no second query
    is spent finding that out."""
    engine, counter = counted
    async with committed_catalogs(
        migrated_database, [link_document("Suggested probe")], recommended=True
    ) as (catalog_id,):
        admin = create_async_engine(migrated_database)
        try:
            async with admin.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE catalogs SET status = :s, published_at = NULL, recommended = true "
                        "WHERE id = :i"
                    ),
                    {"s": CatalogStatus.SUGGESTED.value, "i": catalog_id},
                )
        finally:
            await admin.dispose()

        assert not await read_catalog(engine, catalog_id)
        assert await read_feed(engine) == 0

    assert len(counter.statements) == 2  # one per read, none for the status check
    assert counter.transaction_control == []


async def test_a_catalog_with_every_kind_of_child_is_still_one_statement(
    migrated_database: str, counted: tuple[AsyncEngine, Statements]
) -> None:
    engine, counter = counted
    document = link_document(
        "Full probe",
        kind=["open", "public", "academic", "school", "specialized"],
        publicationTypes=["ebook", "audiobook", "comic"],
        supportedLanguages=["fr", "en", "de"],
        rel_extra=[{"href": f"https://full.example/{n}", "rel": "alternate"} for n in range(20)],
    )
    async with committed_catalogs(migrated_database, [document], recommended=True) as (cid,):
        assert await read_catalog(engine, cid)

    assert len(counter.statements) == 1


async def test_the_import_path_is_not_the_public_read(
    migrated_database: str, settings: Settings
) -> None:
    """The importers' lookups stay ORM (`selectinload`): several statements, on purpose, because
    they hand back objects the import then changes."""
    async with (
        committed_catalogs(
            migrated_database, [link_document("Importer probe")], recommended=True
        ) as (cid,),
        read_engine_with(settings) as engine,
    ):
        counter = Statements(engine)
        async with build_session_factory(engine)() as session:
            loaded = await CatalogRepository(session).fetch_catalog_by_identity_id(cid)
        assert loaded is not None
        assert len(counter.statements) > 1


# --- the real measure: round trips on the wire ----------------------------------------------------


@pytest.fixture
async def proxied(settings: Settings) -> AsyncIterator[tuple[AsyncEngine, WireLog]]:
    """A read engine whose every byte to the server passes the counting proxy."""
    async with counting_proxy(settings) as (through, log), read_engine_with(through) as engine:
        yield engine, log


async def test_a_warm_feed_sends_at_most_two_messages_to_the_server(
    seeded: list[uuid.UUID], proxied: tuple[AsyncEngine, WireLog]
) -> None:
    engine, log = proxied
    await read_feed(engine)  # opens the connection
    before = log.mark()

    assert await read_feed(engine) == SEED_CATALOG_COUNT

    sent = log.since(before)
    assert 1 <= len(sent) <= WARM_READ_MAX_MESSAGES, [chunk[:60] for chunk in sent]


async def test_a_warm_catalog_sends_at_most_two_messages_to_the_server(
    seeded: list[uuid.UUID], proxied: tuple[AsyncEngine, WireLog]
) -> None:
    engine, log = proxied
    await read_catalog(engine, seeded[0])
    before = log.mark()

    assert await read_catalog(engine, seeded[1])

    sent = log.since(before)
    assert 1 <= len(sent) <= WARM_READ_MAX_MESSAGES, [chunk[:60] for chunk in sent]


async def test_fifty_warm_feeds_cost_at_most_two_messages_each(
    seeded: list[uuid.UUID], proxied: tuple[AsyncEngine, WireLog]
) -> None:
    engine, log = proxied
    await read_feed(engine)
    before = log.mark()

    for _ in range(50):
        await read_feed(engine)

    assert log.round_trips - before <= 50 * WARM_READ_MAX_MESSAGES


async def test_no_begin_commit_rollback_savepoint_or_pre_ping_is_ever_sent(
    seeded: list[uuid.UUID], proxied: tuple[AsyncEngine, WireLog]
) -> None:
    engine, log = proxied

    await read_feed(engine)
    warm = log.mark()
    for _ in range(3):
        await read_feed(engine)
        await read_catalog(engine, seeded[0])
    sent = b"".join(log.flights).upper()

    for word in (b"BEGIN", b"COMMIT", b"ROLLBACK", b"SAVEPOINT", b"SET TRANSACTION"):
        assert word not in sent, word
    assert b"SELECT 1" not in b"".join(log.since(warm)).upper()


async def test_the_message_count_does_not_depend_on_the_number_of_catalogs(
    migrated_database: str, proxied: tuple[AsyncEngine, WireLog]
) -> None:
    engine, log = proxied
    await read_feed(engine)
    empty = log.mark()
    await read_feed(engine)
    empty_count = len(log.since(empty))

    documents = [link_document(f"Wire {n:02d}") for n in range(MANY)]
    async with committed_catalogs(migrated_database, documents, recommended=True):
        full = log.mark()
        assert await read_feed(engine) == MANY
        full_count = len(log.since(full))

    assert full_count == empty_count


async def test_a_cold_feed_sends_a_small_bounded_number_of_messages(
    seeded: list[uuid.UUID], proxied: tuple[AsyncEngine, WireLog]
) -> None:
    engine, log = proxied

    await read_feed(engine)

    assert 1 < log.round_trips <= COLD_READ_MAX_MESSAGES, log.round_trips
    assert not log.contains(b"BEGIN", b"COMMIT", b"ROLLBACK")


async def test_the_importer_uses_many_statements_where_the_feed_uses_one(
    seeded: list[uuid.UUID], settings: Settings
) -> None:
    """Control: the same counters see the ORM load's several statements, so a count of 1 above is
    a measurement and not a blind spot."""
    async with read_engine_with(settings) as engine:
        counter = Statements(engine)
        async with build_session_factory(engine)() as session:
            assert await CatalogRepository(session).fetch_catalog_by_identity_id(seeded[0])

    assert len(counter.statements) >= 5
