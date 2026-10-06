"""A connection the database dropped is retried once; nothing else is (ADR-060).

The search pool has no pre-ping, so a pooled connection that Cloud SQL closed while it sat idle
fails the first statement sent on it. SQLAlchemy reports that as `connection_invalidated`, and the
repository repeats the (read-only) statement once on a fresh connection.

The drop is real: `pg_terminate_backend` from another connection, found by the engine's own
`application_name`, so the next checkout is provably the dead backend.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from registry.core.config import Settings
from registry.db.session import build_session_factory
from registry.domain.search_query import parse_search_query
from registry.repositories.search_repository import CatalogSearchRepository
from tests.search_helpers import committed_catalogs, search_engine_with

pytestmark = pytest.mark.integration

QUERY = parse_search_query("tapir")


def documents() -> list[dict[str, Any]]:
    return [
        {
            "metadata": {"title": f"tapir shelf {n}", "kind": ["public"]},
            "links": [{"href": f"https://tapir.example/{n}", "rel": "catalog"}],
        }
        for n in range(3)
    ]


async def titles(session: AsyncSession, *, limit: int = 50) -> tuple[int, list[str]]:
    page = await CatalogSearchRepository(session).search_catalogs(QUERY, limit=limit, offset=0)
    return page.total, [c.title for c in page.catalogs]


async def terminate_backends(url: str, application_name: str) -> int:
    """Kill every backend of the engine under test; returns how many were killed."""
    observer = create_async_engine(url)
    try:
        async with observer.connect() as connection:
            rows = await connection.scalars(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE application_name = :n AND pid <> pg_backend_pid()"
                ),
                {"n": application_name},
            )
            return len(rows.all())
    finally:
        await observer.dispose()


async def test_a_search_on_a_terminated_connection_succeeds_through_the_one_retry(
    settings: Settings, migrated_database: str
) -> None:
    name = f"retry-{uuid.uuid4().hex[:8]}"
    async with (
        committed_catalogs(migrated_database, documents()),
        search_engine_with(settings, application_name=name) as engine,
    ):
        factory = build_session_factory(engine)
        async with factory() as session:
            first = await titles(session)
        killed = await terminate_backends(migrated_database, name)

        async with factory() as session:
            second = await titles(session)

    assert killed == 1  # one pooled connection, now dead
    assert first[0] == 3
    assert second == first


async def test_the_retry_ran_on_a_new_backend(settings: Settings, migrated_database: str) -> None:
    name = f"retry-{uuid.uuid4().hex[:8]}"
    async with (
        committed_catalogs(migrated_database, documents()),
        search_engine_with(settings, application_name=name) as engine,
    ):
        factory = build_session_factory(engine)
        async with engine.connect() as connection:
            before = await connection.scalar(text("SELECT pg_backend_pid()"))
        await terminate_backends(migrated_database, name)

        async with factory() as session:
            await titles(session)
        async with engine.connect() as connection:
            after = await connection.scalar(text("SELECT pg_backend_pid()"))

    assert after != before


async def test_a_failure_that_is_not_a_dropped_connection_is_not_retried(
    settings: Settings,
) -> None:
    """PostgreSQL rejects a negative LIMIT: a real, non-connection error. One statement, then it
    propagates unchanged."""
    async with search_engine_with(settings) as engine:
        seen: list[str] = []
        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            lambda _c, _cur, statement, *_a: seen.append(statement),
        )
        async with build_session_factory(engine)() as session:
            with pytest.raises(DBAPIError) as raised:
                await CatalogSearchRepository(session).search_catalogs(QUERY, limit=-1, offset=0)

    assert not raised.value.connection_invalidated
    assert getattr(raised.value.orig, "sqlstate", None) == "2201W"  # invalid_row_count_in_limit
    assert len(seen) == 1


async def test_the_pool_is_usable_after_a_dropped_connection_was_replaced(
    settings: Settings, migrated_database: str
) -> None:
    name = f"retry-{uuid.uuid4().hex[:8]}"
    async with (
        committed_catalogs(migrated_database, documents()),
        search_engine_with(settings, application_name=name) as engine,
    ):
        factory = build_session_factory(engine)
        async with factory() as session:
            await titles(session)
        await terminate_backends(migrated_database, name)
        results = []
        for _ in range(3):
            async with factory() as session:
                results.append(await titles(session))

    assert results == [(3, results[0][1])] * 3


# --- the retry is bounded: a stub that always reports an invalidated connection ------------------


class AlwaysDropped:
    """A session whose every `execute` fails like a dead connection."""

    def __init__(self) -> None:
        self.executes = 0
        self.rollbacks = 0

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        self.executes += 1
        raise DBAPIError("SELECT", {}, ConnectionResetError("gone"), connection_invalidated=True)

    async def rollback(self) -> None:
        self.rollbacks += 1


async def test_the_retry_happens_at_most_once() -> None:
    stub = AlwaysDropped()
    repository = CatalogSearchRepository(stub)  # type: ignore[arg-type]

    with pytest.raises(DBAPIError) as raised:
        await repository.search_catalogs(QUERY, limit=50, offset=0)

    assert raised.value.connection_invalidated
    assert stub.executes == 2
    assert stub.rollbacks == 1


class DroppedOnce(AlwaysDropped):
    async def execute(self, *_args: object, **_kwargs: object) -> object:
        self.executes += 1
        if self.executes == 1:
            raise DBAPIError(
                "SELECT", {}, ConnectionResetError("gone"), connection_invalidated=True
            )

        class Result:
            def mappings(self) -> "Result":
                return self

            def all(self) -> list[dict[str, Any]]:
                return [{"total": 0, "catalog_id": None}]

        return Result()


async def test_a_second_attempt_that_works_is_returned_after_one_rollback() -> None:
    stub = DroppedOnce()

    page = await CatalogSearchRepository(stub).search_catalogs(  # type: ignore[arg-type]
        QUERY, limit=50, offset=0
    )

    assert (page.total, page.catalogs) == (0, ())
    assert (stub.executes, stub.rollbacks) == (2, 1)


async def test_an_error_that_is_not_invalidated_is_raised_at_the_first_attempt() -> None:
    class Plain(AlwaysDropped):
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            self.executes += 1
            raise DBAPIError("SELECT", {}, RuntimeError("syntax"), connection_invalidated=False)

    stub = Plain()
    with pytest.raises(DBAPIError):
        await CatalogSearchRepository(stub).search_catalogs(  # type: ignore[arg-type]
            QUERY, limit=50, offset=0
        )

    assert (stub.executes, stub.rollbacks) == (1, 0)
