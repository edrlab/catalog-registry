"""One statement, one message: what a search costs the database connection (ADR-060).

Search used to be 7 ORM statements, which was 14 messages on the wire (a pre-ping, BEGIN, SET
TRANSACTION, the settings call, the search, the catalog load, five child loads, COMMIT, and
asyncpg's type look-ups). Now the page and every child come back from one statement, the search
engine is AUTOCOMMIT with its settings on the connection, and nothing else is sent.

Two counters, because each sees what the other cannot:

* `before_cursor_execute` on the search engine counts SQLAlchemy's statements, whatever the page
  size. It cannot see what the driver sends by itself.
* A TCP proxy on loopback (`tests/wire_proxy.py`) counts the chunks the client sends to the
  server. This is the real measure, and the one that would have caught the original 14.

These tests use their own pools and really commit their rows (deleted in a `finally`): the
rollback fixtures are invisible to a second connection, and the proxy needs real connections.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from registry.core.config import Settings
from registry.db.session import build_session_factory, create_database_engine
from registry.domain.search_query import parse_search_query
from registry.repositories.search_repository import CatalogSearchRepository
from tests.read_helpers import Statements
from tests.search_helpers import committed_catalogs, search_engine_with
from tests.wire_proxy import WireLog, counting_proxy

pytestmark = pytest.mark.integration

MATCHES = 55

#: Measured on 7 October 2026 against Postgres 18: 1 for a warm search. 2 leaves room for one
#: driver bookkeeping message; 14 was the old design.
WARM_SEARCH_MAX_MESSAGES = 2
#: A new connection's one-off cost, measured at 20: the startup packet, two SCRAM messages,
#: SQLAlchemy's dialect probes (version, schema, isolation level, string conformance), asyncpg's
#: type introspection and `jit` setting, and the search itself. Paid once per pooled connection
#: (recycled every 5 minutes), never per search.
COLD_SEARCH_MAX_MESSAGES = 25


def quokka_documents() -> list[dict[str, object]]:
    return [
        {
            "metadata": {"title": f"quokka shelf {n}", "kind": ["public"]},
            "links": [
                {
                    "href": f"https://quokka.example/{n}",
                    "type": "application/opds+json",
                    "rel": "catalog",
                }
            ],
        }
        for n in range(MATCHES)
    ]


@pytest.fixture
async def quokkas(migrated_database: str) -> AsyncIterator[None]:
    async with committed_catalogs(migrated_database, quokka_documents()):
        yield


@pytest.fixture
async def counted_search(
    settings: Settings,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], Statements]]:
    async with search_engine_with(settings) as engine:
        counter = Statements(engine)
        yield build_session_factory(engine), counter


async def run_search(
    factory: async_sessionmaker[AsyncSession], query: str, *, limit: int, offset: int = 0
) -> tuple[int, int]:
    async with factory() as session:
        page = await CatalogSearchRepository(session).search_catalogs(
            parse_search_query(query), limit=limit, offset=offset
        )
    return page.total, len(page.catalogs)


@pytest.mark.parametrize("limit", [1, 2, 50])
async def test_one_search_is_one_statement_whatever_the_page_size(
    quokkas: None, counted_search: tuple[async_sessionmaker[AsyncSession], Statements], limit: int
) -> None:
    factory, counter = counted_search

    total, returned = await run_search(factory, "quokka", limit=limit)

    assert (total, returned) == (MATCHES, limit)
    assert len(counter.statements) == 1
    assert counter.statements[0].lstrip().startswith("WITH q AS")


async def test_a_search_with_no_match_is_one_statement(
    quokkas: None, counted_search: tuple[async_sessionmaker[AsyncSession], Statements]
) -> None:
    factory, counter = counted_search

    assert await run_search(factory, "xyzzy", limit=50) == (0, 0)
    assert len(counter.statements) == 1


async def test_a_page_past_the_end_is_one_statement(
    quokkas: None, counted_search: tuple[async_sessionmaker[AsyncSession], Statements]
) -> None:
    factory, counter = counted_search

    assert await run_search(factory, "quokka", limit=50, offset=5000) == (MATCHES, 0)
    assert len(counter.statements) == 1


async def test_no_transaction_control_reaches_the_driver(
    quokkas: None, counted_search: tuple[async_sessionmaker[AsyncSession], Statements]
) -> None:
    factory, counter = counted_search

    await run_search(factory, "quokka", limit=50)

    for statement in counter.statements:
        assert statement.split(None, 1)[0].upper() not in {
            "BEGIN",
            "COMMIT",
            "ROLLBACK",
            "SAVEPOINT",
            "RELEASE",
            "SET",
        }


# --- the real measure: messages on the wire ---------------------------------------------------


@pytest.fixture
async def proxied(settings: Settings) -> AsyncIterator[tuple[AsyncEngine, WireLog]]:
    """A search engine whose every byte to the server passes the counting proxy."""
    async with counting_proxy(settings) as (through, log), search_engine_with(through) as engine:
        yield engine, log


async def warm_up(factory: async_sessionmaker[AsyncSession]) -> None:
    await run_search(factory, "quokka", limit=50)


@pytest.mark.parametrize(
    ("query", "limit", "offset"),
    [
        ("quokka", 50, 0),
        ("quokka", 1, 0),
        ("quokka shelf 12", 50, 0),
        ("xyzzy", 50, 0),
        ("quokka", 50, 5000),
        ("quokkas", 50, 0),
    ],
)
async def test_a_warm_search_sends_at_most_two_messages_to_the_server(
    quokkas: None, proxied: tuple[AsyncEngine, WireLog], query: str, limit: int, offset: int
) -> None:
    engine, log = proxied
    factory = build_session_factory(engine)
    await warm_up(factory)
    before = log.mark()

    await run_search(factory, query, limit=limit, offset=offset)

    sent = log.since(before)
    assert 1 <= len(sent) <= WARM_SEARCH_MAX_MESSAGES, [chunk[:60] for chunk in sent]


async def test_fifty_warm_searches_cost_at_most_two_messages_each(
    quokkas: None, proxied: tuple[AsyncEngine, WireLog]
) -> None:
    engine, log = proxied
    factory = build_session_factory(engine)
    await warm_up(factory)
    before = log.mark()

    for _ in range(50):
        await run_search(factory, "quokka", limit=50)

    assert log.to_server - before <= 50 * WARM_SEARCH_MAX_MESSAGES


async def test_no_begin_commit_rollback_or_savepoint_is_ever_sent(
    quokkas: None, proxied: tuple[AsyncEngine, WireLog]
) -> None:
    engine, log = proxied
    factory = build_session_factory(engine)

    await run_search(factory, "quokka", limit=50)
    warm = log.mark()
    for _ in range(3):
        await run_search(factory, "quokka", limit=50)
    sent = b"".join(log.chunks).upper()

    for word in (b"BEGIN", b"COMMIT", b"ROLLBACK", b"SAVEPOINT", b"SET TRANSACTION"):
        assert word not in sent, word
    assert b"SELECT 1" not in b"".join(log.since(warm)).upper()  # no pre-ping


async def test_the_search_itself_is_in_the_messages_and_nothing_per_page_row(
    quokkas: None, proxied: tuple[AsyncEngine, WireLog]
) -> None:
    """The statement text is sent once per search, not once per catalog: a 50-row page and a
    1-row page cost the same number of messages."""
    engine, log = proxied
    factory = build_session_factory(engine)
    await warm_up(factory)

    big = log.mark()
    await run_search(factory, "quokka", limit=50)
    big_count = len(log.since(big))
    small = log.mark()
    await run_search(factory, "quokka", limit=1)
    small_count = len(log.since(small))

    assert big_count == small_count


async def test_a_cold_first_search_sends_a_small_bounded_number_of_messages(
    quokkas: None, proxied: tuple[AsyncEngine, WireLog]
) -> None:
    engine, log = proxied

    await run_search(build_session_factory(engine), "quokka", limit=50)

    assert 1 < log.to_server <= COLD_SEARCH_MAX_MESSAGES, log.to_server
    assert not log.contains(b"BEGIN", b"COMMIT", b"ROLLBACK")


# --- positive control: the proxy does see what the old design sent -------------------------------


async def test_the_proxy_does_see_transaction_control_on_an_ordinary_session(
    quokkas: None, settings: Settings
) -> None:
    """If this fails the proxy is blind and the tests above prove nothing: an ordinary engine
    (pre-ping on, a session transaction) must show its BEGIN, its ping and its ROLLBACK."""
    async with counting_proxy(settings) as (through, log):
        engine = create_database_engine(through)
        try:
            factory = build_session_factory(engine)
            await warm_up(factory)  # opens the connection
            before = log.mark()
            async with factory() as session:
                await session.execute(text("SELECT 1"))
                await session.rollback()
            sent = b"".join(log.since(before)).upper()
        finally:
            await engine.dispose()

    assert b"BEGIN" in sent
    assert b"ROLLBACK" in sent or b"COMMIT" in sent
    assert len(log.since(before)) > WARM_SEARCH_MAX_MESSAGES
