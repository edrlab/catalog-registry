"""Async engine, session factory, and the request-scoped session dependency."""

from collections.abc import AsyncIterator

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
    lazy refresh, which raises ``MissingGreenlet`` under async — a confusing error with an
    unrelated-looking message."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_database_connection(engine: AsyncEngine) -> None:
    """Raise if the database is unreachable. Used by the readiness probe."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def get_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
