"""Fixture stack

**No mocked database.** A mocked session proves the code calls the methods the mock expects,
which is a tautology; it cannot catch a constraint violation, an enum mismatch, a broken
migration, or an N+1.

**Isolation by rollback, not truncation.** Every test runs inside one outer transaction that
is rolled back at the end. Sessions join it with `join_transaction_mode="create_savepoint"`,
so application code calling `commit()` releases a savepoint and the outer rollback still
undoes everything.

The container-versus-CI-services switch is `TEST_DATABASE_URL`. It is deliberately not
`REGISTRY_`-prefixed: `Settings` uses `extra="forbid"`, so a stray `REGISTRY_*` variable
would fail the application at boot.
"""

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from registry.cli.seed import seed_catalogs
from registry.core.config import Settings
from registry.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_FILE = REPO_ROOT / "data" / "recommended.json"

#: Derived, never hardcoded. Hadrien extends `data/recommended.json`; a test that spells the
#: count out fails on his commit rather than on a defect.
SEED_CATALOG_COUNT = len(json.loads(SEED_FILE.read_text(encoding="utf-8"))["catalogs"])


#: Hosts the suite is allowed to run against without an explicit override.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "db"})

ALLOW_REMOTE = "ALLOW_REMOTE_TEST_DATABASE"


def assert_database_is_disposable(url: str) -> None:
    """Refuse to run the suite against a shared server.

    The suite applies migrations and writes rows. Against the Cloud SQL sandbox that means
    running DDL on a schema someone else is using, the exact "two developers clobbering
    each other" that the conventions keeps local databases for. Set
    ALLOW_REMOTE_TEST_DATABASE=1 to override, deliberately.
    """
    if os.environ.get(ALLOW_REMOTE):
        return

    parts = urlsplit(url)
    host = parts.hostname or ""
    database = parts.path.lstrip("/")

    if host not in LOCAL_HOSTS:
        raise RuntimeError(
            f"TEST_DATABASE_URL points at {host!r}, which is not a local throwaway database. "
            f"The suite runs migrations and writes rows. Set {ALLOW_REMOTE}=1 if you really "
            "mean it."
        )
    # The Cloud SQL Auth Proxy publishes a remote instance on localhost, so the host check
    # alone would wave the sandbox through. Every legitimate path names its database for
    # what it is: `test` from testcontainers, `registry_test` in CI and compose.
    if "test" not in database:
        raise RuntimeError(
            f"TEST_DATABASE_URL names the database {database!r}, which does not look like a "
            f"test database. If this is the Cloud SQL Auth Proxy, stop. Set {ALLOW_REMOTE}=1 "
            "to override."
        )


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A running Postgres: supplied by CI, or a container started for this run."""
    supplied = os.environ.get("TEST_DATABASE_URL")
    if supplied:
        assert_database_is_disposable(supplied)
        yield supplied
        return

    # Deferred: CI supplies TEST_DATABASE_URL and must not need testcontainers importable.
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer("postgres:18-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        seed_file=SEED_FILE,
        environment="test",
        base_url="http://testserver",
    )


@pytest.fixture(scope="session")
def migrated_database(settings: Settings) -> str:
    """Apply migrations once per run.

    Migrations, not `metadata.create_all`: a schema built by `create_all` can diverge from
    the one migrations produce, and the divergence surfaces in production.
    """
    url = str(settings.database_url)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        check=True,
        env={**os.environ, "REGISTRY_DATABASE_URL": url},
    )
    return url


@pytest.fixture
async def db_connection(migrated_database: str) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(migrated_database)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()
    await engine.dispose()


@pytest.fixture
def session_factory(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def seeded_catalogs(db_session: AsyncSession) -> int:
    """Every recommended catalog, imported through the real seed path."""
    created, _ = await seed_catalogs(db_session, SEED_FILE)
    await db_session.commit()
    return created


@pytest.fixture
async def app(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[FastAPI]:
    """The real app, with its session factory rebound to the test's transaction."""
    built = create_app(settings)
    async with built.router.lifespan_context(built):
        built.state.session_factory = session_factory
        yield built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """In-process HTTP. No socket, no port, no race on startup."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as async_client:
        yield async_client


class QueryCounter:
    """Counts ORM statements. That is a rule; this is what makes it a failing test."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, *_args: object, **_kwargs: object) -> None:
        self.count += 1


@pytest.fixture
def query_counter(db_session: AsyncSession) -> Iterator[QueryCounter]:
    counter = QueryCounter()
    event.listen(db_session.sync_session, "do_orm_execute", counter)
    yield counter
    event.remove(db_session.sync_session, "do_orm_execute", counter)
