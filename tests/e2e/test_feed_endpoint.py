"""The feed endpoint, over the real ASGI path.

The seed set is built to demonstrate `Accept-Language` filtering:

| Catalog | Declares | Covers |
|---|---|---|
| Project Gutenberg | nothing | unscoped, never filtered out, always first |
| Librivox | nothing | a second unscoped catalog, so the bucket's own order is visible |
| Standard Ebooks | `en` | a single-language catalog |
| Ebooks libres et gratuits | `fr` | a second, so preference order is visible |

This is the project's seed file, not a fixture written for these tests, so it constrains what
can be asserted here. Specificity tie-breaks and multi-language catalogs need rows it does not
contain; those live in `tests/unit/test_feed_service.py`, against a fake built per rule.

A catalog declaring nothing has made no claim to contradict, so it is kept for every request.
"""

import pytest
from httpx import AsyncClient

from registry.core.constants import OPDS_CATALOG_MEDIA_TYPE
from tests.conftest import SEED_CATALOG_COUNT

pytestmark = pytest.mark.e2e

#: Scoped to no language, so never filtered out, and sorts **above** every language-scoped
#: catalog whatever was asked for. Breadth first.
UNSCOPED = ["Librivox", "Project Gutenberg"]


async def titles(client: AsyncClient, header: str | None = None) -> list[str]:
    headers = {"Accept-Language": header} if header is not None else {}
    response = await client.get("/", headers=headers)
    return [catalog["metadata"]["title"] for catalog in response.json()["catalogs"]]


async def test_the_feed_returns_200_with_no_headers(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    assert (await client.get("/")).status_code == 200


async def test_the_content_type_is_the_opds_media_type(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    response = await client.get("/")

    assert response.headers["content-type"].startswith(OPDS_CATALOG_MEDIA_TYPE)


async def test_no_accept_language_returns_everything_recommended(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    assert await titles(client) == [
        "Ebooks libres et gratuits",
        "Librivox",
        "Project Gutenberg",
        "Standard Ebooks",
    ]


async def test_english_keeps_the_english_catalog(client: AsyncClient, seeded_catalogs: int) -> None:
    assert await titles(client, "en") == [*UNSCOPED, "Standard Ebooks"]


async def test_a_language_no_catalog_declares_keeps_only_the_unscoped_ones(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """The English catalog drops. The two that declare nothing stay, because they have made
    no claim to contradict."""
    assert await titles(client, "ja") == UNSCOPED


async def test_a_regional_range_reaches_a_plain_tag(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """`en-GB` in the header against `en` in the data.

    A reader asking for a regional variant and being shown nothing is the failure this
    guards against, and it needs both RFC 4647 procedures rather than either alone.
    """
    assert await titles(client, "en-GB") == [*UNSCOPED, "Standard Ebooks"]


async def test_rejecting_a_language_excludes_it(client: AsyncClient, seeded_catalogs: int) -> None:
    """`q=0` is "not acceptable", and it must not read as "no preference stated"."""
    assert await titles(client, "en;q=0") == UNSCOPED
    assert await titles(client, "*;q=0") == UNSCOPED


async def test_a_malformed_header_returns_200_unfiltered(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    response = await client.get("/", headers={"Accept-Language": ";;;,,,"})

    assert response.status_code == 200
    assert response.json()["metadata"]["numberOfItems"] == SEED_CATALOG_COUNT


async def test_no_recommended_catalogs_returns_an_empty_list(client: AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert response.json()["catalogs"] == []
    assert response.json()["metadata"]["numberOfItems"] == 0


async def test_no_internal_field_appears_in_any_response(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """Asserted by key absence rather than by eye."""
    internal = {
        "id",
        "status",
        "recommended",
        "submitter_name",
        "submitter_email",
        "created_at",
        "updated_at",
        "published_at",
    }

    for catalog in (await client.get("/")).json()["catalogs"]:
        assert not internal & set(catalog["metadata"])
        assert not internal & set(catalog)


async def test_coverage_is_omitted_when_it_was_never_declared(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """Emitting `global` here would assert worldwide reach nobody claimed.

    No catalog in the seed declares coverage, so this asserts absence everywhere. A seed row
    that did declare it would make this a stronger test; `tests/integration/test_seed.py`
    covers the declared case directly against the column.
    """
    for catalog in (await client.get("/")).json()["catalogs"]:
        assert "coverage" not in catalog["metadata"]


async def test_color_is_always_emitted(client: AsyncClient, seeded_catalogs: int) -> None:
    """Hadrien writes `"color": "gray"` explicitly in his own data."""
    for catalog in (await client.get("/")).json()["catalogs"]:
        assert catalog["metadata"]["color"]


async def test_two_identical_requests_produce_byte_identical_bodies(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """Fails if any collection is emitted without being sorted first."""
    first = await client.get("/", headers={"Accept-Language": "fr, en"})
    second = await client.get("/", headers={"Accept-Language": "fr, en"})

    assert first.content == second.content


async def test_every_catalog_carries_a_synthesised_self_link_first(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """No seed record has a `self` link; the registry builds them."""
    for catalog in (await client.get("/")).json()["catalogs"]:
        self_link = catalog["links"][0]
        assert self_link["rel"] == "self"
        assert self_link["href"].startswith("http://testserver/catalogs/")
        assert self_link["type"] == OPDS_CATALOG_MEDIA_TYPE
