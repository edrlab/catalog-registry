"""`GET /catalogs/{id}`. What every synthesised `self` link points at."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry.core.constants import OPDS_CATALOG_MEDIA_TYPE, PROBLEM_JSON_MEDIA_TYPE
from registry.db.models.catalog import Catalog
from registry.domain.enums import CatalogStatus

pytestmark = pytest.mark.e2e


async def test_a_self_link_from_the_feed_resolves(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """The feed's own output has to be honest: every self link it emits must work."""
    feed = (await client.get("/")).json()

    for catalog in feed["catalogs"]:
        response = await client.get(catalog["links"][0]["href"])

        assert response.status_code == 200
        assert response.json()["metadata"]["title"] == catalog["metadata"]["title"]


async def test_the_content_type_is_the_opds_media_type(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    feed = (await client.get("/")).json()
    response = await client.get(feed["catalogs"][0]["links"][0]["href"])

    assert response.headers["content-type"].startswith(OPDS_CATALOG_MEDIA_TYPE)


async def test_an_unknown_id_returns_404_problem_json(client: AsyncClient) -> None:
    response = await client.get(f"/catalogs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)


async def test_a_malformed_uuid_returns_422(client: AsyncClient) -> None:
    response = await client.get("/catalogs/not-a-uuid")

    assert response.status_code == 422


async def test_a_suggested_catalog_is_not_publicly_readable(
    client: AsyncClient, db_session: AsyncSession, seeded_catalogs: int
) -> None:
    """`GET /catalogs/{id}` is unauthenticated, and an id is not an access control.

    A suggested catalog is somebody's unreviewed submission. It must not be readable just
    because its identifier is known.
    """
    catalog = (await db_session.scalars(select(Catalog))).first()
    assert catalog is not None
    catalog.status = CatalogStatus.SUGGESTED
    catalog.published_at = None
    await db_session.commit()

    assert (await client.get(f"/catalogs/{catalog.id}")).status_code == 404
