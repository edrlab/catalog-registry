"""Repository behaviour, including the N+1 guard. `conventions/testing.md` §5.6, §7."""

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from registry.db.models.catalog import Catalog
from registry.domain.enums import CatalogColor, CatalogStatus, CoverageScope
from registry.rendering.feed_renderer import render_feed
from registry.repositories.catalog_repository import CatalogRepository
from tests.conftest import SEED_CATALOG_COUNT, QueryCounter

pytestmark = pytest.mark.integration

#: One for the catalogs, one per eager-loaded collection. R4 says this must not grow with n.
MAX_FEED_QUERIES = 6


async def test_fetch_recommended_catalogs_returns_the_seeded_set(
    db_session: AsyncSession, seeded_catalogs: int
) -> None:
    catalogs = await CatalogRepository(db_session).fetch_recommended_catalogs()

    assert [catalog.title for catalog in catalogs] == [
        "Librivox",
        "Lirtuel",
        "Project Gutenberg",
    ]


async def test_a_suggested_catalog_is_excluded_even_when_recommended(
    db_session: AsyncSession, seeded_catalogs: int
) -> None:
    catalog = (await CatalogRepository(db_session).fetch_recommended_catalogs())[0]
    catalog.status = CatalogStatus.SUGGESTED
    catalog.published_at = None
    await db_session.commit()

    remaining = await CatalogRepository(db_session).fetch_recommended_catalogs()

    assert len(remaining) == SEED_CATALOG_COUNT - 1


async def test_an_unrecommended_catalog_is_excluded(
    db_session: AsyncSession, seeded_catalogs: int
) -> None:
    catalog = (await CatalogRepository(db_session).fetch_recommended_catalogs())[0]
    catalog.recommended = False
    await db_session.commit()

    assert (
        len(await CatalogRepository(db_session).fetch_recommended_catalogs())
        == SEED_CATALOG_COUNT - 1
    )


async def test_the_feed_query_count_does_not_grow_with_the_number_of_catalogs(
    db_session: AsyncSession, seeded_catalogs: int, query_counter: QueryCounter
) -> None:
    """R4, made executable. Without `selectinload` this is O(n) and the test fails."""
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

    query_counter.count = 0
    catalogs = await CatalogRepository(db_session).fetch_recommended_catalogs()
    # Rendering touches every collection; a missing eager load raises here, not silently.
    render_feed(catalogs, base_url="http://testserver")

    assert len(catalogs) == SEED_CATALOG_COUNT + 20
    assert query_counter.count <= MAX_FEED_QUERIES
