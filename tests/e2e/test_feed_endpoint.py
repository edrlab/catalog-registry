"""the conventions.5 and §5.7, over the real ASGI path.

The seed set is built to demonstrate `Accept-Language` filtering:

| Catalog | Declares | Covers |
|---|---|---|
| Project Gutenberg | nothing | unscoped, never filtered out, always first |
| Librivox | nothing | a second unscoped catalog, so the bucket's own order is visible |

The seed is `demo/catalogs/`. Hadrien's three fixtures, minus the `self` links he authors for
the published output.

**Most ordering cases are not testable here**, because only one catalog declares a language:
preference-order reversal, specificity tie-breaks and cross-boundary ranking all need
catalogs this set does not contain. Those live in `tests/unit/test_feed_service.py`, against
a fake built for each rule.

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


async def test_french_keeps_the_french_catalog_and_drops_the_english_one(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    assert await titles(client, "fr") == [*UNSCOPED, "Ebooks libres et gratuits"]


async def test_english_keeps_the_english_catalog_and_drops_the_french_one(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    assert await titles(client, "en") == [*UNSCOPED, "Standard Ebooks"]


async def test_a_language_nobody_declares_keeps_only_the_unscoped_catalogs(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    assert await titles(client, "ja") == UNSCOPED


async def test_quality_orders_the_preferred_language_first(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """The v0 criterion: `en;q=0.8, fr;q=0.9` orders `fr` ahead of `en`.

    Membership is unchanged by quality, both languages are acceptable, but the order is
    not. French outranks English because the client said so.
    """
    assert await titles(client, "en;q=0.8, fr;q=0.9") == [
        *UNSCOPED,  # scoped to no language, so relevant whatever was asked
        "Ebooks libres et gratuits",  # fr, q=0.9
        "Standard Ebooks",  # en, q=0.8
    ]


async def test_reversing_the_qualities_reverses_the_order(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    assert await titles(client, "en;q=0.9, fr;q=0.8") == [
        *UNSCOPED,
        "Standard Ebooks",
        "Ebooks libres et gratuits",
    ]


async def test_a_regional_request_still_finds_the_base_language(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """`fr-BE` must reach a catalog declaring plain `fr`. RFC 4647 Lookup truncation.

    A Belgian reader asking for `fr-BE` and being shown no French catalog at all is the
    failure this guards against.
    """
    assert await titles(client, "fr-BE") == [*UNSCOPED, "Ebooks libres et gratuits"]


async def test_header_order_is_the_preference_when_no_q_is_given(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """`fr, en` carries no `q`, so both ranges are q=1.0 and only the written order says
    French is preferred. Ranking on `q` alone would lose that."""
    assert await titles(client, "fr, en") == [
        *UNSCOPED,
        "Ebooks libres et gratuits",  # fr, the range written first
        "Standard Ebooks",  # en, the range written second
    ]


async def test_a_regional_range_matches_a_plain_tag_in_our_data(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """`fr-FR` in the header against `fr` in the data, the case Hadrien named."""
    assert await titles(client, "fr-FR") == [*UNSCOPED, "Ebooks libres et gratuits"]


async def test_equal_rank_keeps_the_database_order(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """The sort is stable, so catalogs ranking equally stay in the title order SQL gave."""
    assert await titles(client, "fr") == [*UNSCOPED, "Ebooks libres et gratuits"]


async def test_rejecting_french_excludes_the_french_catalog(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    assert "Ebooks libres et gratuits" not in await titles(client, "fr;q=0, en")


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
