"""Repository behaviour, including the N+1 guard.

The public reads (`fetch_recommended_catalogs`, `fetch_catalog_by_id`) return fresh transient
snapshots built from one statement each (ADR-062). They are not attached to the session, so a
change is made the way the importer makes it (`fetch_catalog_by_identity_id`, attached) and is
seen by the NEXT read, never by an object already returned.
"""

import datetime
from collections.abc import Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from registry.db.models.catalog import Catalog
from registry.domain.enums import CatalogColor, CatalogStatus, CoverageScope
from registry.rendering.feed_renderer import render_feed
from registry.repositories.catalog_repository import CatalogRepository
from tests.conftest import SEED_CATALOG_COUNT, SEED_TITLES_IN_FILE_ORDER

pytestmark = pytest.mark.integration


class SelectCounter:
    """Counts the SELECTs that reach the driver on the test's connection. SAVEPOINT and RELEASE,
    which the rollback fixture's session adds around a commit, are not reads."""

    def __init__(self) -> None:
        self.selects: list[str] = []

    def __call__(self, _conn: object, _cursor: object, statement: str, *_a: object) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            self.selects.append(statement)


@pytest.fixture
def select_counter(db_connection: AsyncConnection) -> Iterator[SelectCounter]:
    counter = SelectCounter()
    event.listen(db_connection.sync_engine, "before_cursor_execute", counter)
    yield counter
    event.remove(db_connection.sync_engine, "before_cursor_execute", counter)


async def test_fetch_recommended_catalogs_returns_the_seeded_set(
    db_session: AsyncSession, seeded_catalogs: int
) -> None:
    catalogs = await CatalogRepository(db_session).fetch_recommended_catalogs()

    # File order, not alphabetical: the seed staggers `created_at` by position, and this
    # query orders on it. Title is still the last `ORDER BY` term, for determinism only.
    assert [catalog.title for catalog in catalogs] == SEED_TITLES_IN_FILE_ORDER


async def test_a_suggested_catalog_is_excluded_even_when_recommended(
    db_session: AsyncSession, seeded_catalogs: int
) -> None:
    repository = CatalogRepository(db_session)
    first = (await repository.fetch_recommended_catalogs())[0]
    # The importer's attached object: the public read's are snapshots that change nothing.
    attached = await repository.fetch_catalog_by_identity_id(first.id)
    assert attached is not None
    attached.status = CatalogStatus.SUGGESTED
    attached.published_at = None
    await db_session.commit()

    remaining = await repository.fetch_recommended_catalogs()

    assert len(remaining) == SEED_CATALOG_COUNT - 1
    assert first.id not in {catalog.id for catalog in remaining}
    assert await repository.fetch_catalog_by_id(first.id) is None


async def test_an_unrecommended_catalog_is_excluded(
    db_session: AsyncSession, seeded_catalogs: int
) -> None:
    repository = CatalogRepository(db_session)
    first = (await repository.fetch_recommended_catalogs())[0]
    attached = await repository.fetch_catalog_by_identity_id(first.id)
    assert attached is not None
    attached.recommended = False
    await db_session.commit()

    remaining = await repository.fetch_recommended_catalogs()

    assert len(remaining) == SEED_CATALOG_COUNT - 1
    assert first.id not in {catalog.id for catalog in remaining}
    # Active but not recommended is still a real, published catalog.
    assert await repository.fetch_catalog_by_id(first.id) is not None


async def test_catalogs_with_the_same_time_and_title_come_back_in_id_order(
    db_session: AsyncSession, seeded_catalogs: int
) -> None:
    """A title is not unique, so `created_at, title` alone leaves ties the database may answer in
    any order. The id is the last tie-break, which keeps the feed's bytes the same run after run."""
    repository = CatalogRepository(db_session)
    moment = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    for snapshot in await repository.fetch_recommended_catalogs():
        attached = await repository.fetch_catalog_by_identity_id(snapshot.id)
        assert attached is not None
        attached.title = "Same title"
        attached.created_at = moment
    await db_session.commit()

    ids = [str(catalog.id) for catalog in await repository.fetch_recommended_catalogs()]

    assert len(ids) == SEED_CATALOG_COUNT
    assert ids == sorted(ids)


async def test_a_returned_snapshot_is_not_changed_by_a_later_change_to_the_row(
    db_session: AsyncSession, seeded_catalogs: int
) -> None:
    repository = CatalogRepository(db_session)
    snapshot = (await repository.fetch_recommended_catalogs())[0]
    title = snapshot.title

    attached = await repository.fetch_catalog_by_identity_id(snapshot.id)
    assert attached is not None
    attached.title = "Renamed"
    await db_session.commit()

    assert snapshot.title == title
    renamed = await repository.fetch_catalog_by_id(snapshot.id)
    assert renamed is not None and renamed.title == "Renamed"


async def test_the_feed_is_one_select_whatever_the_number_of_catalogs(
    db_session: AsyncSession, seeded_catalogs: int, select_counter: SelectCounter
) -> None:
    """The N+1 rule (R4), made executable: one statement for the catalogs and every child, so
    it cannot grow with n. (`test_read_one_statement` proves it at the driver and on the wire.)"""
    for index in range(20):
        db_session.add(
            Catalog(
                title=f"Bulk {index:02d}",
                status=CatalogStatus.ACTIVE,
                published_at=datetime.datetime.now(datetime.UTC),
                recommended=True,
                color=CatalogColor.GRAY,
                coverage=CoverageScope.GLOBAL,
            )
        )
    await db_session.commit()

    select_counter.selects.clear()
    catalogs = await CatalogRepository(db_session).fetch_recommended_catalogs()
    # Rendering touches every collection; a transient object cannot lazy-load, so a missing
    # child would show here as an error or an empty list, not silently.
    render_feed(catalogs, base_url="http://testserver")

    assert len(catalogs) == SEED_CATALOG_COUNT + 20
    assert len(select_counter.selects) == 1
