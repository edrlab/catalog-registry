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

import asyncio
import ipaddress
import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg  # type: ignore[import-untyped]
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from registry.cli.seed import seed_catalogs
from registry.core.config import Settings
from registry.main import create_app
from registry.repositories.search_repository import SEARCH_CONNECTION_SETTINGS

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Same image as compose.yaml and CI: Postgres 18 with Lead, the open-source stand-in for TIN.
LEAD_IMAGE = (
    "planetscale/lead:pg18@sha256:ebb795acccd7f7bdabc009ca9490b73c4dbb216f61bc4ebcc4d95e284cc96b76"
)
SEED_FILE = REPO_ROOT / "data" / "recommended.json"
LIBRARIES_FILE = REPO_ROOT / "data" / "libraries.json"

#: Derived, never hardcoded. `data/recommended.json` is expected to grow; a test that spells the
#: count out would fail on that commit rather than on a defect.
SEED_CATALOG_COUNT = len(json.loads(SEED_FILE.read_text(encoding="utf-8"))["catalogs"])

#: The file's own order, which the seed turns into the feed's order by staggering
#: `created_at`. Derived for the same reason as the count: reordering the file is how the
#: feed is reordered, so a test that spells the order out would have to be edited to agree
#: with a change it is supposed to be checking.
SEED_TITLES_IN_FILE_ORDER = [
    catalog["metadata"]["title"]
    for catalog in json.loads(SEED_FILE.read_text(encoding="utf-8"))["catalogs"]
]


#: Hosts the suite is allowed to run against without an explicit override.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "db"})

ALLOW_REMOTE = "ALLOW_REMOTE_TEST_DATABASE"


class RemoteNetworkError(RuntimeError):
    """A test tried to reach a host that is not local. Not an `OSError`, so code that treats
    network errors as "unreachable" cannot swallow it."""


def refuse_remote_network() -> None:
    """No test reaches the internet, the live registry or a hosted database.

    Every name lookup and every TCP connect in the test process is checked: a local name (and
    the addresses it resolved to) or a loopback address passes, anything else raises. With
    parallel workers a stray call would otherwise go out once per worker. A deliberate remote
    test database (`ALLOW_REMOTE_TEST_DATABASE=1`) is allowed by its host only.
    """
    allowed_names = set(LOCAL_HOSTS)
    if os.environ.get(ALLOW_REMOTE) and (supplied := os.environ.get("TEST_DATABASE_URL")):
        allowed_names.add(urlsplit(supplied).hostname or "")
    resolved: set[str] = set()
    lookup, connect, connect_ex = (
        socket.getaddrinfo,
        socket.socket.connect,
        socket.socket.connect_ex,
    )

    def address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(host.split("%", maxsplit=1)[0])
        except ValueError:
            return None

    def is_local(host: object) -> bool:
        if not isinstance(host, str):
            return False
        if host in allowed_names or host in resolved:
            return True
        parsed = address(host)
        return parsed is not None and parsed.is_loopback

    def guarded_lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
        host_name = host.decode() if isinstance(host, bytes) else host
        # An address literal is parsed, not looked up: no traffic. Its connect is still checked.
        if host_name is not None and not is_local(host_name) and address(host_name) is None:
            raise RemoteNetworkError(f"a test tried to resolve {host_name!r}")
        answers = lookup(host, *args, **kwargs)
        resolved.update(str(answer[4][0]) for answer in answers)
        return answers

    def checked(address: Any) -> None:
        # A unix socket (Docker's API for testcontainers) is a path, not a host.
        if isinstance(address, tuple) and not is_local(address[0]):
            raise RemoteNetworkError(f"a test tried to connect to {address[0]!r}")

    def guarded_connect(self: socket.socket, address: Any) -> None:
        checked(address)
        connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        checked(address)
        return connect_ex(self, address)

    # Through `vars`, so the type checker does not compare these with the typeshed signatures.
    vars(socket)["getaddrinfo"] = guarded_lookup
    socket.socket.connect = guarded_connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign,assignment]


def pytest_configure(config: pytest.Config) -> None:
    """Runs in every process, the xdist workers included, before any worker starts.

    A parallel run without `TEST_DATABASE_URL` starts one container here, in the controller, and
    hands it to the workers through that variable: one server with a database per worker, as in
    CI. A container per worker would multiply the memory and the start-up for nothing.
    """
    refuse_remote_network()
    controller = "PYTEST_XDIST_WORKER" not in os.environ
    parallel = config.getoption("numprocesses", default=None)
    if parallel and controller and not os.environ.get("TEST_DATABASE_URL"):
        from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415

        # `stop` removes the container and its volume; Ryuk removes them if the run is killed.
        container = PostgresContainer(LEAD_IMAGE, driver="asyncpg").start()
        config.add_cleanup(container.stop)
        os.environ["TEST_DATABASE_URL"] = container.get_connection_url()
        config.add_cleanup(lambda: os.environ.__delitem__("TEST_DATABASE_URL"))


def assert_database_is_disposable(url: str) -> None:
    """Refuse to run the suite against a shared server.

    The suite applies migrations and writes rows. Against a shared database that means
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
    # A tunnel or proxy publishes a remote instance on localhost, so the host check
    # alone would wave it through. Every legitimate path names its database for
    # what it is: `test` from testcontainers, `registry_test` in CI and compose.
    if "test" not in database:
        raise RuntimeError(
            f"TEST_DATABASE_URL names the database {database!r}, which does not look like a "
            f"test database. If this is a tunnel to a remote database, stop. Set {ALLOW_REMOTE}=1 "
            "to override."
        )


def reset_worker_database(url: str, worker: str, *, create: bool) -> str:
    """Drop the database named for the pytest-xdist *worker* on the server *url* names, then
    create it afresh when *create*; returns its URL.

    Workers cannot share one: the lock and race tests commit real rows and hold real locks, and
    each worker migrates its database at start. Dropped first, so a crashed run leaves nothing the
    next one trips over, and again at the end, so a run leaves nothing on the server. The
    migrations create the extensions.
    """
    server = make_url(url)
    name = f"{server.database}_{worker}"

    async def reset() -> None:
        connection = await asyncpg.connect(
            # The maintenance database: the one named in *url* need not exist yet.
            server.set(drivername="postgresql", database="postgres").render_as_string(
                hide_password=False
            )
        )
        try:
            await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            if create:
                await connection.execute(f'CREATE DATABASE "{name}"')
        finally:
            await connection.close()

    asyncio.run(reset())
    return server.set(database=name).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A running Postgres: supplied by CI, or a container started for this run.

    Under pytest-xdist each worker runs this once and gets its own database on the supplied
    server (`pytest_configure` supplies one when nothing else does).
    """
    supplied = os.environ.get("TEST_DATABASE_URL")
    if supplied:
        assert_database_is_disposable(supplied)
        worker = os.environ.get("PYTEST_XDIST_WORKER")
        if not worker:
            yield supplied
            return
        yield reset_worker_database(supplied, worker, create=True)
        reset_worker_database(supplied, worker, create=False)
        return

    # Deferred: CI supplies TEST_DATABASE_URL and must not need testcontainers importable.
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer(LEAD_IMAGE, driver="asyncpg") as container:
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
    # Searches in the tests run on this connection, so it takes the search pool's trigram
    # threshold: without it Postgres's default (0.6) answers, not production's.
    trigram = "pg_trgm.word_similarity_threshold"
    engine = create_async_engine(
        migrated_database,
        connect_args={"server_settings": {trigram: SEARCH_CONNECTION_SETTINGS[trigram]}},
    )
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
async def searchable_catalogs(db_session: AsyncSession) -> None:
    """Both data sets (recommended.json and libraries.json), imported through the real seed
    path. The triggers build the `catalog_search` rows; nothing here touches that table."""
    await seed_catalogs(db_session, SEED_FILE)
    await seed_catalogs(db_session, LIBRARIES_FILE, recommended=False)
    await db_session.commit()


@pytest.fixture
async def app(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    db_connection: AsyncConnection,
) -> AsyncIterator[FastAPI]:
    """The real app, with the feed/catalog and search session factories rebound to the test's
    transaction.

    The read pools' own settings (timeouts, read-only mode) are NOT exercised
    here; the tests that prove them build a real app and engine instead (`test_read_pool*`,
    `test_search_timeout*`, `test_search_engine`).
    """
    built = create_app(settings)
    async with built.router.lifespan_context(built):
        built.state.session_factory = session_factory
        built.state.search_session_factory = session_factory
        yield built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """In-process HTTP. No socket, no port, no race on startup."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as async_client:
        yield async_client


ADMIN_SERVICE_KEY = "test-service-key"


@pytest.fixture
async def admin_app(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    db_connection: AsyncConnection,
) -> AsyncIterator[FastAPI]:
    """The app as a developer runs it locally: a key, and the dev identity.

    Reads and the actor store join the test's transaction, so a first login is rolled back with
    everything else. The token path is covered with its own settings in the unit tests.
    """
    local = settings.model_copy(
        update={
            "environment": "local",
            "service_keys": SecretStr(ADMIN_SERVICE_KEY),
            "admin_dev_identity": True,
            # A developer's own .env may configure tokens; the dev identity is off when it does.
            "admin_token_issuer": None,
            "admin_token_audience": None,
            "admin_jwks_url": None,
        }
    )
    built = create_app(local)
    async with built.router.lifespan_context(built):
        built.state.session_factory = session_factory
        built.state.write_session_factory = session_factory
        yield built


@pytest.fixture
async def admin_client(admin_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=admin_app), base_url="http://testserver"
    ) as async_client:
        yield async_client


def admin_headers(email: str = "viewer@edrlab.org") -> dict[str, str]:
    """What a local caller sends: the key and a dev identity."""
    return {"X-Registry-Service-Key": ADMIN_SERVICE_KEY, "X-Registry-Dev-Identity": email}
