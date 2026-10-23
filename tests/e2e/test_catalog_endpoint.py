"""`GET /catalogs/{id}`. What every synthesised `self` link points at."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import import_catalog_document
from registry.core.constants import OPDS_CATALOG_MEDIA_TYPE, PROBLEM_JSON_MEDIA_TYPE
from registry.db.models.catalog import Catalog
from registry.domain.enums import CatalogStatus

pytestmark = pytest.mark.e2e


async def test_a_self_link_from_the_feed_resolves(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """The feed's own output has to be honest: every self link it emits must work."""
    feed = (await client.get("/", headers={"Accept-Language": "*"})).json()

    for catalog in feed["catalogs"]:
        response = await client.get(catalog["links"][0]["href"])

        assert response.status_code == 200
        assert response.json()["metadata"]["title"] == catalog["metadata"]["title"]


async def test_the_content_type_is_the_opds_media_type(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    feed = (await client.get("/", headers={"Accept-Language": "*"})).json()
    response = await client.get(feed["catalogs"][0]["links"][0]["href"])

    assert response.headers["content-type"].startswith(OPDS_CATALOG_MEDIA_TYPE)


async def test_an_unknown_id_returns_404_problem_json(client: AsyncClient) -> None:
    response = await client.get(f"/catalogs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)


async def test_a_malformed_uuid_returns_422(client: AsyncClient) -> None:
    response = await client.get("/catalogs/not-a-uuid")

    assert response.status_code == 422


async def test_a_draft_catalog_is_not_publicly_readable(
    client: AsyncClient, db_session: AsyncSession, seeded_catalogs: int
) -> None:
    """`GET /catalogs/{id}` is unauthenticated, and an id is not an access control.

    A draft catalog is somebody's unreviewed submission. It must not be readable just
    because its identifier is known.
    """
    catalog = (await db_session.scalars(select(Catalog))).first()
    assert catalog is not None
    catalog.status = CatalogStatus.DRAFT
    catalog.published_at = None
    await db_session.commit()

    assert (await client.get(f"/catalogs/{catalog.id}")).status_code == 404


async def test_a_not_recommended_catalog_is_readable_at_its_derived_id(
    client: AsyncClient, db_session: AsyncSession, seeded_catalogs: int
) -> None:
    """The `data/libraries.json` case, end to end and through HTTP.

    Absent from the feed, present at `/catalogs/{id}`, and the id is computable from the
    catalog's `catalog` href without asking the database, which is the only reason Hadrien can
    fetch one of these at all.
    """
    href = "https://www.lirtuel.be/v1/home.opds2"
    document = {
        "metadata": {"title": "Lirtuel", "kind": ["public"], "country": "BE"},
        "links": [{"href": href, "rel": "catalog"}],
    }
    await import_catalog_document(db_session, document, recommended=False)
    await db_session.commit()
    derived = uuid.uuid5(uuid.NAMESPACE_URL, href)

    feed = (await client.get("/", headers={"Accept-Language": "*"})).json()
    response = await client.get(f"/catalogs/{derived}")

    assert "Lirtuel" not in [catalog["metadata"]["title"] for catalog in feed["catalogs"]]
    assert len(feed["catalogs"]) == seeded_catalogs
    assert response.status_code == 200
    assert response.json()["metadata"]["identifier"] == f"urn:uuid:{derived}"
    assert response.json()["metadata"]["country"] == "BE"


@pytest.mark.parametrize(
    ("status", "served"),
    [
        (CatalogStatus.PUBLISHED, True),
        (CatalogStatus.UNPUBLISHED, True),
        (CatalogStatus.DRAFT, False),
        (CatalogStatus.ARCHIVED, False),
    ],
)
async def test_the_direct_link_serves_published_and_unpublished_only(
    client: AsyncClient,
    db_session: AsyncSession,
    seeded_catalogs: int,
    status: CatalogStatus,
    served: bool,
) -> None:
    catalog = (await db_session.scalars(select(Catalog))).first()
    assert catalog is not None
    catalog.status = status
    await db_session.commit()

    response = await client.get(f"/catalogs/{catalog.id}")

    assert response.status_code == (200 if served else 404)


@pytest.mark.parametrize("status", [CatalogStatus.UNPUBLISHED, CatalogStatus.ARCHIVED])
async def test_an_unpublished_or_archived_catalog_is_in_no_list(
    client: AsyncClient, db_session: AsyncSession, seeded_catalogs: int, status: CatalogStatus
) -> None:
    """Unpublished is reachable by its direct link and nowhere else: not the feed, not search."""
    catalog = (await db_session.scalars(select(Catalog))).first()
    assert catalog is not None
    title = catalog.title
    catalog.status = status
    await db_session.commit()

    feed = (await client.get("/", headers={"Accept-Language": "*"})).json()
    found = (await client.get("/search", params={"query": title})).json()

    assert title not in [c["metadata"]["title"] for c in feed["catalogs"]]
    assert title not in [c["metadata"]["title"] for c in found["catalogs"]]
