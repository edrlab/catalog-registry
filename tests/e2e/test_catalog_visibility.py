"""The lifecycle contract's visibility table, through HTTP, for every status.

The public SQL spells the visible statuses as literals (one statement per read); this pins them
to `LISTED_STATUSES` and `DIRECT_LINK_STATUSES`, so a change to one cannot leave the other behind.
"""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry.db.models import Catalog
from registry.domain.enums import CatalogStatus
from registry.domain.lifecycle import DIRECT_LINK_STATUSES, LISTED_STATUSES

pytestmark = pytest.mark.e2e


def identifiers(body: dict[str, object]) -> set[str]:
    catalogs = body["catalogs"]
    assert isinstance(catalogs, list)
    return {c["metadata"]["identifier"].removeprefix("urn:uuid:") for c in catalogs}


@pytest.mark.parametrize("status", list(CatalogStatus))
async def test_each_status_is_seen_exactly_where_the_contract_says(
    client: AsyncClient, db_session: AsyncSession, seeded_catalogs: int, status: CatalogStatus
) -> None:
    catalog = (await db_session.scalars(select(Catalog).where(Catalog.recommended))).first()
    assert catalog is not None
    catalog_id, title = str(catalog.id), catalog.title
    catalog.status = status
    catalog.published_at = None if status is CatalogStatus.DRAFT else datetime.now(UTC)
    await db_session.commit()

    by_id = await client.get(f"/catalogs/{catalog_id}")
    feed = await client.get("/")
    search = await client.get("/search", params={"query": f'"{title}"'})

    assert by_id.status_code == (200 if status in DIRECT_LINK_STATUSES else 404)
    assert (catalog_id in identifiers(feed.json())) == (status in LISTED_STATUSES)
    assert (catalog_id in identifiers(search.json())) == (status in LISTED_STATUSES)
