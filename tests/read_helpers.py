"""Helpers shared by the suites of the feed and `GET /catalogs/{id}` reads (ADR-062).

Those two reads run on their own pool (`build_read_engine(settings, READ_CONNECTION_SETTINGS)`),
one statement each. Tests of that pool need rows another connection can see, so the rows here are
really committed and deleted in a `finally` (the rollback fixtures are invisible to a second
connection).
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import QueuePool

from registry.cli.seed import seed_catalogs
from registry.core.config import Settings
from registry.db.session import build_read_engine
from registry.repositories.catalog_repository import READ_CONNECTION_SETTINGS


@asynccontextmanager
async def read_engine_with(settings: Settings, **overrides: str) -> AsyncIterator[AsyncEngine]:
    """The production feed/catalog pool, with some server settings replaced.

    A short `statement_timeout` makes a slow feed fail in 150 ms instead of 10 s; an
    `application_name` lets a test find (and terminate) exactly this engine's backends.
    """
    engine = build_read_engine(settings, {**READ_CONNECTION_SETTINGS, **overrides})
    try:
        yield engine
    finally:
        await engine.dispose()


@asynccontextmanager
async def committed_seed(url: str, *files: tuple[Path, bool]) -> AsyncIterator[list[uuid.UUID]]:
    """Seed files really committed, each as `(path, recommended)`; every id they created is deleted
    by id in a `finally` (children and search rows go with them: foreign keys cascade)."""
    engine = create_async_engine(url)
    ids: list[uuid.UUID] = []
    try:
        async with engine.connect() as connection:
            before = set((await connection.scalars(text("SELECT id FROM catalogs"))).all())
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            for path, recommended in files:
                await seed_catalogs(session, path, recommended=recommended)
            await session.commit()
        async with engine.connect() as connection:
            ids = [
                found
                for found in (await connection.scalars(text("SELECT id FROM catalogs"))).all()
                if found not in before
            ]
        yield ids
    finally:
        async with engine.begin() as cleanup:
            await cleanup.execute(text("DELETE FROM catalogs WHERE id = ANY(:ids)"), {"ids": ids})
        await engine.dispose()


class Statements:
    """What SQLAlchemy sends to the driver on one engine (`before_cursor_execute`)."""

    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self.statements: list[str] = []
        if engine is not None:
            event.listen(engine.sync_engine, "before_cursor_execute", self)

    def __call__(self, _conn: object, _cursor: object, statement: str, *_a: object) -> None:
        self.statements.append(statement)

    def detach(self, engine: AsyncEngine) -> None:
        event.remove(engine.sync_engine, "before_cursor_execute", self)

    @property
    def transaction_control(self) -> list[str]:
        words = {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "SET"}
        return [s for s in self.statements if s.split(None, 1)[0].upper() in words]


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


def link_document(
    title: str, *, rel_extra: list[dict[str, Any]] | None = None, **meta: Any
) -> dict[str, Any]:
    """A minimal valid catalog document with a unique identity href."""
    return {
        "metadata": {"title": title, "kind": ["public"], **meta},
        "links": [
            {
                "href": f"https://{uuid.uuid4().hex}.example/opds",
                "type": "application/opds+json",
                "rel": "catalog",
            },
            *(rel_extra or []),
        ],
    }


@asynccontextmanager
async def locked(url: str, table: str) -> AsyncIterator[None]:
    """Hold ACCESS EXCLUSIVE on *table* from another connection until the block ends. Nothing is
    committed; the lock is rolled back in a `finally`. Only a table name from this suite."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as locker:
            await locker.execute(text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
            try:
                yield
            finally:
                await locker.rollback()
    finally:
        await engine.dispose()


class StubResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "StubResult":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class StubSession:
    """A session whose `execute` fails *failures* times with *error*, then returns *rows*."""

    def __init__(
        self,
        error: Exception | None = None,
        *,
        failures: int = 0,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.error = error
        self.failures = failures
        self.rows = rows or []
        self.executes = 0
        self.rollbacks = 0

    async def execute(self, *_args: object, **_kwargs: object) -> StubResult:
        self.executes += 1
        if self.error is not None and self.executes <= self.failures:
            raise self.error
        return StubResult(self.rows)

    async def rollback(self) -> None:
        self.rollbacks += 1


def checked_out(engine: AsyncEngine) -> int:
    """Connections of *engine*'s pool that are in use right now: 0 once every request is done."""
    pool = engine.sync_engine.pool
    assert isinstance(pool, QueuePool)
    return pool.checkedout()
