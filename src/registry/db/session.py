"""Async engine and session factories, one for reads, one for writes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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


@asynccontextmanager
async def read_only_transaction(
    factory: async_sessionmaker[AsyncSession], *, statement_timeout_ms: int
) -> AsyncIterator[AsyncSession]:
    """A read-only transaction with a statement timeout (ADR-058).

    Not the read factory: under its AUTOCOMMIT, `SET LOCAL statement_timeout` is silently
    ignored and a slow query runs to completion (tested). Inside a transaction the setting is
    enforced, and being transaction-local it never leaks to the pooled connection.
    """
    async with factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(statement_timeout_ms)},
        )
        yield session
