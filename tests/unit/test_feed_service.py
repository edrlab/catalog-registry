"""The feed service, against an in-memory fake.

A fake that satisfies the Protocol is checked by the type system; a mock is not. No database
is touched here, that is the whole point of the service depending on `CatalogReader`.

**This is where the ordering rules are pinned down**, rather than in the e2e tests. The seed
holds three catalogs chosen to be a realistic starting registry; the fake here holds whatever
each rule needs to be provable, including a French-only catalog the seed no longer has. The
current rule:

    Use all languages listed in `Accept-Language`. Since this can contain regional
    preferences, match against those (for example "fr-FR" in `Accept-Language` but "FR" in
    our data). Language preferences are ranked in `Accept-Language`, so respect that
    preference in the order of recommended catalogs displayed. Primary sorting order:
    language, with catalogs not scoped to a specific language listed *below* the matched
    ones. Secondary sort order: `created_at`, newest first.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from registry.db.models.catalog import Catalog, CatalogLanguageRow
from registry.domain.enums import CatalogColor, CatalogStatus, CoverageScope
from registry.services.feed_service import resolve_top_level_feed

pytestmark = pytest.mark.unit

#: Every catalog built without an explicit `created_at` gets this same instant, so tests that
#: aren't about recency don't have to think about it — ties fall back to build order via a
#: stable sort, same as before `created_at` existed as a sort key.
_DEFAULT_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class FakeCatalogReader:
    """Satisfies `registry.repositories.protocols.CatalogReader`."""

    def __init__(self, catalogs: Sequence[Catalog]) -> None:
        self._catalogs = catalogs

    async def fetch_recommended_catalogs(self) -> Sequence[Catalog]:
        return self._catalogs


def build_catalog(
    title: str, *languages: str, created_at: datetime = _DEFAULT_CREATED_AT
) -> Catalog:
    # Server defaults apply on INSERT, so a transient instance has to state them itself.
    catalog = Catalog(
        title=title,
        status=CatalogStatus.ACTIVE,
        recommended=True,
        color=CatalogColor.GRAY,
        coverage=CoverageScope.GLOBAL,
        created_at=created_at,
    )
    catalog.kinds = []
    catalog.publication_types = []
    catalog.subdivisions = []
    catalog.links = []
    catalog.languages = [CatalogLanguageRow(language_tag=tag) for tag in languages]
    return catalog


#: Deliberately built in a title order that is *not* the expected output for most headers, so
#: a test cannot pass by accident of the input ordering.
READER = FakeCatalogReader(
    [build_catalog("English", "en"), build_catalog("French", "fr"), build_catalog("Silent")]
)


async def titles(accept_language: str | None, reader: FakeCatalogReader = READER) -> list[str]:
    feed = await resolve_top_level_feed(
        reader, accept_language=accept_language, base_url="http://testserver"
    )
    return [entry["metadata"]["title"] for entry in feed["catalogs"]]


async def test_no_header_returns_everything() -> None:
    assert await titles(None) == ["English", "French", "Silent"]


async def test_a_stated_language_keeps_matching_and_unscoped_catalogs() -> None:
    """The French match leads; `Silent`, scoped to no language, follows."""
    assert await titles("fr") == ["French", "Silent"]


async def test_a_language_nobody_declares_keeps_only_unscoped_catalogs() -> None:
    assert await titles("ja") == ["Silent"]


async def test_a_wildcard_keeps_everything() -> None:
    assert await titles("*") == ["English", "French", "Silent"]


async def test_header_order_decides_when_no_quality_is_given() -> None:
    assert await titles("fr, en") == ["French", "English", "Silent"]
    assert await titles("en, fr") == ["English", "French", "Silent"]


async def test_quality_overrides_header_order() -> None:
    assert await titles("en, fr;q=1.0") == ["English", "French", "Silent"]
    assert await titles("en;q=0.5, fr") == ["French", "English", "Silent"]


async def test_a_malformed_header_is_treated_as_no_preference() -> None:
    assert await titles(";;;,,,") == ["English", "French", "Silent"]


async def test_number_of_items_matches_the_catalogs_returned() -> None:
    feed = await resolve_top_level_feed(READER, accept_language="fr", base_url="http://testserver")

    assert feed["metadata"]["numberOfItems"] == len(feed["catalogs"]) == 2


async def test_the_self_link_is_absolute_and_built_from_the_base_url() -> None:
    feed = await resolve_top_level_feed(
        READER, accept_language=None, base_url="https://example.org/"
    )

    assert feed["links"][0] == {
        "href": "https://example.org/",
        "type": "application/opds-catalog+json",
        "rel": "self",
    }


# --- regional ranges against plain tags, and the reverse --------------------------------


@pytest.mark.parametrize(
    "header",
    ["fr", "fr-FR", "fr-BE", "FR", "fr-Latn-FR", "fr-fr"],
)
async def test_any_french_range_reaches_a_catalog_declaring_plain_fr(header: str) -> None:
    """The header may be more specific than our data, or differently cased. Both must match.

    `fr-Latn-FR` also has to work: it is three subtags deep, and the catalog holds one.
    """
    assert await titles(header) == ["French", "Silent"]


async def test_a_plain_range_reaches_a_regional_catalog() -> None:
    """The other direction. Asking for `fr`, holding `fr-BE`."""
    reader = FakeCatalogReader([build_catalog("Belgian", "fr-be"), build_catalog("Silent")])

    assert await titles("fr", reader) == ["Belgian", "Silent"]


async def test_sibling_regions_do_not_match_each_other() -> None:
    """`fr-BE` and `fr-CA` share an ancestor but neither is a prefix of the other.

    Matching them would mean a Belgian reader being offered a Canadian catalog as though it
    were the same request.
    """
    reader = FakeCatalogReader([build_catalog("Canadian", "fr-ca"), build_catalog("Silent")])

    assert await titles("fr-be", reader) == ["Silent"]


async def test_a_near_miss_prefix_is_not_a_match() -> None:
    """`fr` must not match `frr` (Northern Frisian), that is a character comparison."""
    reader = FakeCatalogReader([build_catalog("Frisian", "frr"), build_catalog("Silent")])

    assert await titles("fr", reader) == ["Silent"]


# --- preference order ------------------------------------------------------------------


async def test_the_full_preference_order_is_respected() -> None:
    """Every range is used, and each catalog lands at the position of the range it matched."""
    reader = FakeCatalogReader(
        [
            build_catalog("German", "de"),
            build_catalog("English", "en"),
            build_catalog("French", "fr"),
            build_catalog("Silent"),
        ]
    )

    assert await titles("fr;q=0.9, de;q=0.8, en;q=0.7", reader) == [
        "French",
        "German",
        "English",
        "Silent",
    ]


async def test_a_catalog_is_ranked_on_its_best_language_not_its_first() -> None:
    """A catalog offering several languages takes the best position any of them earns."""
    reader = FakeCatalogReader([build_catalog("Both", "en", "fr"), build_catalog("English", "en")])

    assert await titles("fr, en", reader) == ["Both", "English"]


async def test_offering_an_extra_language_never_demotes_a_catalog() -> None:
    reader = FakeCatalogReader(
        [build_catalog("French", "fr"), build_catalog("French plus", "fr", "de")]
    )

    assert await titles("fr", reader) == ["French", "French plus"]


async def test_specificity_breaks_ties_within_one_range() -> None:
    reader = FakeCatalogReader([build_catalog("Plain", "fr"), build_catalog("Regional", "fr-be")])

    assert await titles("fr-be", reader) == ["Regional", "Plain"]


async def test_specificity_never_crosses_a_preference_boundary() -> None:
    """`en-GB` matches more precisely, but the client ranked French higher."""
    reader = FakeCatalogReader([build_catalog("British", "en-gb"), build_catalog("French", "fr")])

    assert await titles("en-gb;q=0.8, fr;q=0.9", reader) == ["French", "British"]


async def test_a_wildcard_ranks_at_its_own_position() -> None:
    """`fr` first, then everything else, the wildcard does not promote the rest."""
    reader = FakeCatalogReader([build_catalog("German", "de"), build_catalog("French", "fr")])

    assert await titles("fr, *", reader) == ["French", "German"]


# --- the unscoped bucket, and the created_at tiebreaker --------------------------------


async def test_unscoped_catalogs_trail_regardless_of_how_good_the_matches_are() -> None:
    """An exact, top-quality match still sorts above every catalog scoped to no language."""
    reader = FakeCatalogReader(
        [build_catalog("Exact", "fr-be"), build_catalog("Silent"), build_catalog("Other")]
    )

    assert await titles("fr-be", reader) == ["Exact", "Silent", "Other"]


async def test_unscoped_catalogs_sort_newest_first_among_themselves() -> None:
    """Ties within the unscoped bucket break on `created_at`, newest first — not the order
    the reader happened to return them in."""
    base = _DEFAULT_CREATED_AT
    reader = FakeCatalogReader(
        [
            build_catalog("Older", created_at=base),
            build_catalog("Newer", created_at=base + timedelta(days=1)),
            build_catalog("French", "fr"),
        ]
    )

    assert await titles("fr", reader) == ["French", "Newer", "Older"]


async def test_created_at_breaks_ties_within_a_language_match_too() -> None:
    """Two catalogs matching the same range at the same depth: the newer one wins."""
    base = _DEFAULT_CREATED_AT
    reader = FakeCatalogReader(
        [
            build_catalog("Older", "fr", created_at=base),
            build_catalog("Newer", "fr", created_at=base + timedelta(days=1)),
        ]
    )

    assert await titles("fr", reader) == ["Newer", "Older"]


async def test_an_empty_registry_is_not_an_error() -> None:
    assert await titles("fr", FakeCatalogReader([])) == []
