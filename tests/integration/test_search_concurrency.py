"""Two transactions adding different subdivisions to one catalog (CodeRabbit finding).

Without a lock, each transaction rebuilds the catalog's `catalog_search` row without seeing
the other's uncommitted subdivision, and the last writer drops the other's names.
`refresh_catalog_search` now takes `FOR NO KEY UPDATE` on the catalog row first, so the second
rebuild waits for the first commit and then sees it.

This test really commits, from three separate connections (two writers and an observer), on
the migrated database: the rollback fixtures cannot show a lock wait between transactions.
Everything it creates is deleted in a `finally`, keyed by one id, and the database holds no
other catalogs at that point, since every other test rolls back.

The test proves its own point rather than trusting the final document alone: a third
connection watches `pg_blocking_pids` and the test only lets the first transaction commit once
it has seen the second one blocked behind it. If the lock were removed, the second transaction
would not block, that observation would time out, and the assertions on the wait and on the
final document would both fail.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration

OBSERVE_FOR_SECONDS = 5.0


@pytest.fixture
async def engines(migrated_database: str) -> AsyncIterator[list[AsyncEngine]]:
    created = [create_async_engine(migrated_database) for _ in range(3)]
    yield created
    for engine in created:
        await engine.dispose()


async def wait_until_blocked(observer: AsyncConnection, victim: int, blocker: int) -> bool:
    """Poll `pg_blocking_pids` until *victim* is waiting on *blocker*, or give up."""
    deadline = time.monotonic() + OBSERVE_FOR_SECONDS
    while time.monotonic() < deadline:
        blocked_by = await observer.scalar(text("SELECT pg_blocking_pids(:v)"), {"v": victim})
        if blocker in (blocked_by or []):
            return True
        await asyncio.sleep(0.02)
    return False


async def test_two_transactions_adding_different_subdivisions_both_end_up_in_the_document(
    engines: list[AsyncEngine],
) -> None:
    first_engine, second_engine, observer_engine = engines
    catalog_id = uuid.uuid4()
    first_inserted = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    timeline: dict[str, float] = {}
    pids: dict[str, int] = {}

    async def first_writer() -> None:
        async with first_engine.connect() as connection:
            pids["first"] = int(await connection.scalar(text("SELECT pg_backend_pid()")) or 0)
            await connection.execute(
                text(
                    "INSERT INTO catalog_subdivisions (catalog_id, subdivision_code) "
                    "VALUES (:c, 'BE-WAL')"
                ),
                {"c": catalog_id},
            )
            first_inserted.set()
            await release_first.wait()
            await connection.commit()
            timeline["first_committed"] = time.monotonic()

    async def second_writer() -> None:
        await first_inserted.wait()
        async with second_engine.connect() as connection:
            pids["second"] = int(await connection.scalar(text("SELECT pg_backend_pid()")) or 0)
            second_started.set()
            # Blocks inside the trigger's lock until the first transaction commits.
            await connection.execute(
                text(
                    "INSERT INTO catalog_subdivisions (catalog_id, subdivision_code) "
                    "VALUES (:c, 'BE-BRU')"
                ),
                {"c": catalog_id},
            )
            timeline["second_insert_returned"] = time.monotonic()
            await connection.commit()

    async def conductor(observer: AsyncConnection) -> bool:
        await second_started.wait()
        blocked = await wait_until_blocked(observer, pids["second"], pids["first"])
        # Release either way, so a failing run still finishes and the `finally` cleans up.
        release_first.set()
        return blocked

    try:
        async with observer_engine.connect() as observer:
            await observer.execute(text("SELECT 1"))
            async with observer_engine.begin() as setup:
                await setup.execute(
                    text(
                        "INSERT INTO catalogs (id, title, status, published_at) "
                        "VALUES (:c, 'Concurrency probe', 'active', now())"
                    ),
                    {"c": catalog_id},
                )
            _, _, was_blocked = await asyncio.wait_for(
                asyncio.gather(first_writer(), second_writer(), conductor(observer)), timeout=30
            )
            await observer.rollback()

            async with observer_engine.connect() as reader:
                row = (
                    await reader.execute(
                        text(
                            "SELECT document::text AS document, names FROM catalog_search "
                            "WHERE catalog_id = :c"
                        ),
                        {"c": catalog_id},
                    )
                ).one()
                subdivisions = (
                    (
                        await reader.execute(
                            text(
                                "SELECT subdivision_code FROM catalog_subdivisions "
                                "WHERE catalog_id = :c ORDER BY 1"
                            ),
                            {"c": catalog_id},
                        )
                    )
                    .scalars()
                    .all()
                )
    finally:
        async with observer_engine.begin() as cleanup:
            await cleanup.execute(text("DELETE FROM catalogs WHERE id = :c"), {"c": catalog_id})

    assert was_blocked, "the second transaction was never blocked behind the first"
    assert timeline["second_insert_returned"] >= timeline["first_committed"]
    assert subdivisions == ["BE-BRU", "BE-WAL"]
    for word in ("wallonie", "bruxelles"):
        assert f"'{word}'" in row.document, word
        assert word in row.names, word


async def test_the_probe_leaves_nothing_behind(engines: list[AsyncEngine]) -> None:
    async with engines[0].connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM catalogs")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM catalog_search")) == 0
