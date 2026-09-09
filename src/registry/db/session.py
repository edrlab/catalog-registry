"""Async engine and session factories, one for reads, one for writes."""

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
