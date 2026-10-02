"""R4 on the search path: the same number of round trips whatever the page size.

One search is 1 statement for the matches, 1 to load the page's catalogs, and 5 `selectinload`s
(kinds, publication types, languages, subdivisions, links) = 7.

What `do_orm_execute` counts: every `Session.execute()`, including a Core `text()` statement,
so the raw search SQL IS counted (measured: 7, the same as the cursor-level count). It does not
count `BEGIN`, `SAVEPOINT` or what the session does on commit. A second counter on the
connection's `before_cursor_execute` sees what actually reaches the driver, and the two agree.
"""

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from registry.domain.search_query import parse_search_query
from registry.repositories.search_repository import CatalogSearchRepository
from tests.conftest import QueryCounter
from tests.search_helpers import add_synthetic_catalogs

pytestmark = pytest.mark.integration

ROUND_TRIPS = 1 + 1 + 5


class CursorStatements:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, _conn: object, _cursor: object, statement: str, *_a: object) -> None:
        self.statements.append(statement)

    def data_statements(self) -> list[str]:
        """Without the savepoint plumbing the rollback-isolation fixture adds."""
        return [s for s in self.statements if s.split(None, 1)[0] not in {"SAVEPOINT", "RELEASE"}]


@pytest.fixture
def cursor_statements(db_connection: AsyncConnection) -> Iterator[CursorStatements]:
    recorder = CursorStatements()
    event.listen(db_connection.sync_connection, "before_cursor_execute", recorder)
    yield recorder
    event.remove(db_connection.sync_connection, "before_cursor_execute", recorder)


@pytest.fixture
async def fifty_five_matches(db_session: AsyncSession, searchable_catalogs: None) -> None:
    await add_synthetic_catalogs(db_session, 55, "quokka")


@pytest.mark.parametrize("limit", [1, 2, 50])
async def test_one_search_is_seven_round_trips_whatever_the_page_size(
    db_session: AsyncSession,
    fifty_five_matches: None,
    query_counter: QueryCounter,
    cursor_statements: CursorStatements,
    limit: int,
) -> None:
    query_counter.count = 0
    cursor_statements.statements.clear()

    page = await CatalogSearchRepository(db_session).search_catalogs(
        parse_search_query("quokka"), limit=limit, offset=0
    )

    assert len(page.catalogs) == limit
    assert page.total == 55
    assert query_counter.count == ROUND_TRIPS
    assert len(cursor_statements.data_statements()) == ROUND_TRIPS


async def test_the_statements_are_the_search_then_the_load_then_five_collections(
    db_session: AsyncSession, fifty_five_matches: None, cursor_statements: CursorStatements
) -> None:
    cursor_statements.statements.clear()

    await CatalogSearchRepository(db_session).search_catalogs(
        parse_search_query("quokka"), limit=50, offset=0
    )

    statements = cursor_statements.data_statements()
    assert statements[0].lstrip().startswith("WITH q AS")
    assert [s.split("FROM")[1].split()[0] for s in statements[1:]] == [
        "catalogs",
        "catalog_kinds",
        "catalog_publication_types",
        "catalog_languages",
        "catalog_subdivisions",
        "links",
    ]


async def test_a_search_with_no_match_is_one_round_trip_and_loads_nothing(
    db_session: AsyncSession,
    searchable_catalogs: None,
    query_counter: QueryCounter,
) -> None:
    query_counter.count = 0

    page = await CatalogSearchRepository(db_session).search_catalogs(
        parse_search_query("xyzzy"), limit=50, offset=0
    )

    assert (page.total, page.catalogs) == (0, ())
    assert query_counter.count == 1


async def test_a_page_past_the_end_is_one_round_trip(
    db_session: AsyncSession, fifty_five_matches: None, query_counter: QueryCounter
) -> None:
    query_counter.count = 0

    page = await CatalogSearchRepository(db_session).search_catalogs(
        parse_search_query("quokka"), limit=50, offset=5000
    )

    assert page.total == 55
    assert page.catalogs == ()
    assert query_counter.count == 1


@pytest.mark.parametrize("query", ["quokka", "quokka shelf 12"])
async def test_a_search_request_over_http_adds_nothing_to_the_seven(
    client: AsyncClient,
    fifty_five_matches: None,
    cursor_statements: CursorStatements,
    query: str,
) -> None:
    """Through the app: 7 data statements, plus the read-only setup the transaction runs."""
    cursor_statements.statements.clear()

    response = await client.get("/search", params={"query": query})

    assert response.status_code == 200
    plumbing = ("SET TRANSACTION", "SELECT set_config")
    data = [s for s in cursor_statements.data_statements() if not s.startswith(plumbing)]
    assert len(data) == ROUND_TRIPS
