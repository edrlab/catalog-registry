"""upgrade head, downgrade to the v0.1 head, upgrade head: the same schema and the same rows.

Runs in a scratch database created for the test and dropped after it, on the same server as
the shared test database, never in it: a downgrade in the shared one would break every other
test running against it. The two catalog data files are seeded first, so the round trip is also
checked against real rows: the downgrade must leave them alone and the second upgrade must
rebuild all of `catalog_search` from them.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from registry.cli.seed import seed_catalogs
from tests.conftest import LIBRARIES_FILE, REPO_ROOT, SEED_FILE
from tests.search_helpers import ALL_TITLES

pytestmark = pytest.mark.integration

V01_HEAD = "b41f7c9ade52"

#: One query per kind of object the migrations create. Ordered, so two snapshots compare equal.
SCHEMA_QUERIES = {
    "columns": (
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name <> 'alembic_version' ORDER BY 1, ordinal_position"
    ),
    "indexes": (
        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "ORDER BY 1, 2"
    ),
    "constraints": (
        "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
    ),
    "triggers": (
        "SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid) FROM pg_trigger "
        "WHERE NOT tgisinternal ORDER BY 1, 2"
    ),
    "functions": (
        "SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p "
        "WHERE p.pronamespace = 'public'::regnamespace "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') "
        "ORDER BY 1"
    ),
    "search_configuration": (
        "SELECT m.maptokentype, m.mapseqno, m.mapdict::regdictionary::text "
        "FROM pg_ts_config_map m JOIN pg_ts_config c ON c.oid = m.mapcfg "
        "WHERE c.cfgname = 'registry_simple' ORDER BY 2, 3"
    ),
    "extensions": "SELECT extname FROM pg_extension ORDER BY 1",
    "tables": "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1",
}

DATA_QUERIES = {
    "catalog_search": "SELECT catalog_id, document::text, names FROM catalog_search ORDER BY 1",
    "catalogs": "SELECT id, title FROM catalogs ORDER BY 1",
    "country_languages": "SELECT * FROM country_languages ORDER BY 1, 2",
    "country_names": "SELECT * FROM country_names ORDER BY 1, 2, 3",
    "subdivision_names": "SELECT * FROM subdivision_names ORDER BY 1, 2, 3",
    "country_subdivision_types": "SELECT * FROM country_subdivision_types ORDER BY 1, 2",
    "subdivisions": "SELECT code, country_alpha2, subdivision_type FROM subdivisions ORDER BY 1",
}


def alembic(url: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        env={**os.environ, "REGISTRY_DATABASE_URL": url},
    )


async def snapshot(
    engine: AsyncEngine, queries: dict[str, str]
) -> dict[str, list[tuple[object, ...]]]:
    async with engine.connect() as connection:
        return {
            name: [tuple(row) for row in await connection.execute(text(query))]
            for name, query in queries.items()
        }


@pytest.fixture
async def scratch_url(migrated_database: str) -> AsyncIterator[str]:
    """A throwaway database on the test server, named for what it is, dropped afterwards."""
    name = f"registry_roundtrip_test_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(migrated_database, isolation_level="AUTOCOMMIT")
    try:
        try:
            async with admin.connect() as connection:
                await connection.execute(text(f'CREATE DATABASE "{name}"'))
        except ProgrammingError:
            pytest.skip("the test database user cannot CREATE DATABASE")
        try:
            yield (
                make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
            )
        finally:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def test_upgrade_downgrade_upgrade_leaves_the_schema_and_the_search_rows_identical(
    scratch_url: str,
) -> None:
    alembic(scratch_url, "upgrade", "head")
    engine = create_async_engine(scratch_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            await seed_catalogs(session, SEED_FILE)
            await seed_catalogs(session, LIBRARIES_FILE, recommended=False)
            await session.commit()
        schema_before = await snapshot(engine, SCHEMA_QUERIES)
        data_before = await snapshot(engine, DATA_QUERIES)
        assert len(data_before["catalog_search"]) == len(ALL_TITLES) == 12

        alembic(scratch_url, "downgrade", V01_HEAD)

        schema_down = await snapshot(engine, SCHEMA_QUERIES)
        assert "catalog_search" not in {row[0] for row in schema_down["tables"]}
        assert {row[0] for row in schema_down["functions"]}.isdisjoint(
            {"fold", "refresh_catalog_search", "registry_query"}
        )
        async with engine.connect() as connection:
            # The downgrade never touches v0 rows.
            assert await connection.scalar(text("SELECT count(*) FROM catalogs")) == len(ALL_TITLES)

        alembic(scratch_url, "upgrade", "head")

        assert await snapshot(engine, SCHEMA_QUERIES) == schema_before
        assert await snapshot(engine, DATA_QUERIES) == data_before
    finally:
        await engine.dispose()
