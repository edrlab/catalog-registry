"""Async engine and session factories, one for reads, one for writes."""

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from registry.core.config import Settings

#: Short enough that an idle connection is replaced before an intermediary can drop it. A drop that
#: still happens is retried once by `fetch_rows` (one statement per read makes that safe).
POOL_RECYCLE_SECONDS = 300


def create_database_engine(settings: Settings) -> AsyncEngine:
    """Writes (the importers, admin) and the readiness probe. Recycled like the read pools: a
    hosted database may close a connection held for too long."""
    return create_async_engine(
        str(settings.database_url),
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """``expire_on_commit=False``: otherwise any attribute access after commit triggers a
    lazy refresh, which raises ``MissingGreenlet`` under async, a confusing error with an
    unrelated-looking message."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_database_connection(engine: AsyncEngine) -> None:
    """Raise if the database is unreachable. Used by the readiness probe."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


def build_read_engine(settings: Settings, server_settings: Mapping[str, str]) -> AsyncEngine:
    """A connection pool for the public reads: search, the feed, one catalog.

    Each of these is one read statement, so it needs no transaction: AUTOCOMMIT saves BEGIN and
    COMMIT, and the limits ride on the connection itself instead of being set again for every
    request: *server_settings* are sent once, in the startup packet (`statement_timeout`,
    `default_transaction_read_only`, and for search the typo threshold). A pool per kind of read, so
    one kind's limits cannot reach another's. No pre-ping (3 extra round trips per request):
    connections are recycled and one retry covers a dropped one. Writes (the importers) and the
    readiness probe stay on the main engine, `create_database_engine`.
    """
    return create_async_engine(
        str(settings.database_url),
        pool_pre_ping=False,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        isolation_level="AUTOCOMMIT",
        connect_args={"server_settings": dict(server_settings)},
    )
