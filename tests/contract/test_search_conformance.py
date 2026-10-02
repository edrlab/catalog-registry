"""Search output against the repository's own schemas (R7) and the no-leak rule (R3).

A search result is the same catalog the detail endpoint serves, so each one is also compared
with `GET /catalogs/{id}`: rendered identically, field for field.
"""

import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.core.constants import OPDS_CATALOG_MEDIA_TYPE
from tests.search_helpers import assert_valid_catalog, assert_valid_feed

pytestmark = pytest.mark.contract

SPREAD = [
    "paris",
    "bibliotheque",
    "belgique",
    "Île de France",
    "wallis",
    "ebooks",
    "France",
    "gutenbrg",
    "bibliothèque -paris",
    '"numérique de paris"',
    "paris OR valais",
    "xyzzy",
    "",
    "& | ! :",
    "de",
]

#: Columns and derived fields that must never be on the wire.
INTERNAL_KEYS = {
    "id",
    "status",
    "recommended",
    "created_at",
    "createdAt",
    "updated_at",
    "updatedAt",
    "published_at",
    "publishedAt",
    "submitter_name",
    "submitter_email",
    "submitterName",
    "submitterEmail",
    "country_code",
    "document",
    "names",
    "score",
    "tier",
    "rank",
}


def all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in all_keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in all_keys(v)}
    return set()


@pytest.fixture
async def with_submitter_details(db_session: AsyncSession, searchable_catalogs: None) -> None:
    """Give every catalog submitter data and a distinctive description, so a leak is visible."""
    await db_session.execute(
        text(
            "UPDATE catalogs SET submitter_name = 'Secret Submitter', "
            "submitter_email = 'secret@submitter.example'"
        )
    )


@pytest.mark.parametrize("query", SPREAD, ids=[q or "(empty)" for q in SPREAD])
async def test_every_search_response_validates_against_the_feed_and_catalog_schemas(
    client: AsyncClient, searchable_catalogs: None, query: str
) -> None:
    response = await client.get("/search", params={"query": query})

    assert response.status_code == 200
    body = response.json()
    assert_valid_feed(body)
    for catalog in body["catalogs"]:
        assert_valid_catalog(catalog)


@pytest.mark.parametrize("page", [1, 2, 1000])
async def test_pages_past_the_end_validate_too(
    client: AsyncClient, searchable_catalogs: None, page: int
) -> None:
    response = await client.get("/search", params={"query": "bibliotheque", "page": page})

    assert_valid_feed(response.json())


@pytest.mark.parametrize("query", SPREAD, ids=[q or "(empty)" for q in SPREAD])
async def test_no_internal_field_reaches_a_search_response(
    client: AsyncClient, with_submitter_details: None, query: str
) -> None:
    response = await client.get("/search", params={"query": query})

    body = response.json()
    assert not all_keys(body) & INTERNAL_KEYS, all_keys(body) & INTERNAL_KEYS
    raw = json.dumps(body)
    assert "Secret Submitter" not in raw
    assert "secret@submitter.example" not in raw
    for catalog in body["catalogs"]:
        assert set(catalog) <= {"metadata", "links", "publications", "navigation"}


async def test_every_result_is_rendered_exactly_as_the_detail_endpoint_renders_it(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    compared = 0
    for query in ("bibliotheque", "belgique", "ebooks", "france", "gutenbrg", "valais", "paris"):
        for catalog in (await client.get("/search", params={"query": query})).json()["catalogs"]:
            catalog_id = catalog["metadata"]["identifier"].removeprefix("urn:uuid:")
            detail = await client.get(f"/catalogs/{catalog_id}")
            assert detail.status_code == 200
            assert catalog == detail.json(), catalog["metadata"]["title"]
            compared += 1

    assert compared >= 10


async def test_a_search_result_equals_the_same_catalog_in_the_top_level_feed(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    feed = {c["metadata"]["title"]: c for c in (await client.get("/")).json()["catalogs"]}
    found = (await client.get("/search", params={"query": "ebooks"})).json()["catalogs"]

    assert found
    for catalog in found:
        assert feed[catalog["metadata"]["title"]] == catalog


async def test_the_search_response_is_an_opds_catalog_with_the_security_headers(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    response = await client.get("/search", params={"query": "paris"})

    assert response.headers["content-type"].startswith(OPDS_CATALOG_MEDIA_TYPE)
    assert response.headers["x-content-type-options"] == "nosniff"
