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


def create_database_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.database_url), pool_pre_ping=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """``expire_on_commit=False``: otherwise any attribute access after commit triggers a
    lazy refresh, which raises ``MissingGreenlet`` under async, a confusing error with an
    unrelated-looking message."""
    return async_sessionmaker(engine, expire_on_commit=False)


def build_read_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions for the read path, which opens no transaction.

    A feed request only reads, but a default session wraps it in `BEGIN` … `ROLLBACK`. **Reads
    only**. Writes build their own factory, and v1.0's write routes must not use this.
    """
    return async_sessionmaker(
        engine.execution_options(isolation_level="AUTOCOMMIT"), expire_on_commit=False
    )


async def check_database_connection(engine: AsyncEngine) -> None:
    """Raise if the database is unreachable. Used by the readiness probe."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


#: Short enough that an idle connection is replaced before an intermediary can drop it. A drop that
#: still happens is retried once by the repository (one statement per search makes that safe).
SEARCH_POOL_RECYCLE_SECONDS = 300


def build_search_engine(settings: Settings, server_settings: Mapping[str, str]) -> AsyncEngine:
    """The connection pool search runs on (ADR-058, amended).

    Search is one read statement, so it needs no transaction: AUTOCOMMIT saves BEGIN and COMMIT, and
    its limits ride on the connection itself instead of being set again for every request:
    *server_settings* are sent once, in the startup packet, as `statement_timeout`,
    `default_transaction_read_only` and the typo threshold. A dedicated pool, so none of this can
    reach the feed or the importers. No pre-ping (3 extra round trips per request): connections are
    recycled and one retry covers a dropped one.
    """
    return create_async_engine(
        str(settings.database_url),
        pool_pre_ping=False,
        pool_recycle=SEARCH_POOL_RECYCLE_SECONDS,
        isolation_level="AUTOCOMMIT",
        connect_args={"server_settings": dict(server_settings)},
    )
