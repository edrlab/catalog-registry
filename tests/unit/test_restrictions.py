"""`restrict`: parsing, the link form, and the two rules on plain values."""

import re
import uuid
from datetime import UTC, datetime

import pytest

from registry.domain.catalog_view import CatalogView, KindRow, LinkRow, PublicationTypeRow
from registry.domain.enums import CatalogColor, CatalogKind, LinkRel, PublicationType
from registry.domain.restrictions import (
    NO_RESTRICTIONS,
    RESTRICT_PATTERN,
    Restrictions,
    allows,
    is_opds2_media_type,
    parse_restrictions,
)

pytestmark = pytest.mark.unit

OPDS2 = "application/opds+json"


def catalog(
    kinds: tuple[CatalogKind, ...],
    *links: tuple[LinkRel, str | None],
    types: tuple[PublicationType, ...] = (),
) -> CatalogView:
    return CatalogView(
        id=uuid.uuid4(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        title="t",
        description=None,
        color=CatalogColor.GRAY,
        country_code=None,
        city=None,
        coverage=None,
        kinds=[KindRow(k) for k in kinds],
        publication_types=[PublicationTypeRow(t) for t in types],
        languages=[],
        subdivisions=[],
        links=[
            LinkRow(f"https://x/{i}", mt, rel, False, None) for i, (rel, mt) in enumerate(links)
        ],
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, NO_RESTRICTIONS),
        ("", NO_RESTRICTIONS),
        ("opds", Restrictions(opds=True)),
        ("no-open", Restrictions(no_open=True)),
        ("opds,no-open", Restrictions(opds=True, no_open=True)),
        ("no-open,opds", Restrictions(opds=True, no_open=True)),
        ("no-audiobook", Restrictions(no_audiobook=True)),
        ("no-audiobook,opds,no-open", Restrictions(True, True, True)),
    ],
)
def test_parse(value: str | None, expected: Restrictions) -> None:
    assert parse_restrictions(value) == expected


@pytest.mark.parametrize(
    "value", ["", "opds", "no-open", "no-audiobook", "opds,no-open,no-audiobook", "opds,opds"]
)
def test_the_route_pattern_accepts(value: str) -> None:
    assert re.fullmatch(RESTRICT_PATTERN, value)


@pytest.mark.parametrize(
    "value", ["opsd", "OPDS", "opds,", ",opds", "opds, no-open", "all", "no-audio", "audiobook"]
)
def test_the_route_pattern_rejects(value: str) -> None:
    assert not re.fullmatch(RESTRICT_PATTERN, value)


def test_query_value_is_canonical_and_none_when_unrestricted() -> None:
    assert NO_RESTRICTIONS.query_value is None
    assert Restrictions(no_open=True, opds=True).query_value == "opds,no-open"
    assert Restrictions(no_audiobook=True, opds=True).query_value == "opds,no-audiobook"
    assert Restrictions(True, True, True).query_value == "opds,no-open,no-audiobook"


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        (OPDS2, True),
        ("Application/OPDS+JSON", True),
        (f" {OPDS2} ; profile=x", True),
        ("application/opds-catalog+json", False),
        ("application/atom+xml;profile=opds-catalog", False),
        ("text/html", False),
        (None, False),
    ],
)
def test_opds2_media_type(media_type: str | None, expected: bool) -> None:
    assert is_opds2_media_type(media_type) is expected


def test_opds_keeps_a_catalog_with_an_opds2_catalog_link() -> None:
    assert allows(catalog((), (LinkRel.CATALOG, OPDS2)), Restrictions(opds=True))


def test_opds_drops_an_html_only_catalog_and_one_with_no_links() -> None:
    assert not allows(catalog((), (LinkRel.CATALOG, "text/html")), Restrictions(opds=True))
    assert not allows(catalog(()), Restrictions(opds=True))


def test_opds_needs_the_catalog_rel_not_just_any_opds_link() -> None:
    """A `search` or `shelf` link in OPDS is not a place to browse."""
    only_search = catalog((), (LinkRel.CATALOG, "text/html"), (LinkRel.SEARCH, OPDS2))
    assert not allows(only_search, Restrictions(opds=True))


def test_no_open_drops_any_catalog_with_open_among_its_kinds() -> None:
    both = catalog((CatalogKind.OPEN, CatalogKind.PUBLIC))
    assert not allows(both, Restrictions(no_open=True))
    assert allows(catalog((CatalogKind.PUBLIC,)), Restrictions(no_open=True))
    assert allows(catalog(()), Restrictions(no_open=True))


def test_both_restrictions_must_hold() -> None:
    open_opds = catalog((CatalogKind.OPEN,), (LinkRel.CATALOG, OPDS2))
    public_opds = catalog((CatalogKind.PUBLIC,), (LinkRel.CATALOG, OPDS2))
    both = Restrictions(opds=True, no_open=True)
    assert not allows(open_opds, both)
    assert allows(public_opds, both)


def test_no_restrictions_allow_everything() -> None:
    assert allows(catalog((CatalogKind.OPEN,), (LinkRel.CATALOG, "text/html")), NO_RESTRICTIONS)


EBOOK, AUDIO, COMIC = PublicationType.EBOOK, PublicationType.AUDIOBOOK, PublicationType.COMIC


def test_no_audiobook_drops_a_catalog_with_audiobooks_and_nothing_else() -> None:
    assert not allows(catalog((), types=(AUDIO,)), Restrictions(no_audiobook=True))


@pytest.mark.parametrize(
    "types",
    [(EBOOK,), (EBOOK, AUDIO), (EBOOK, COMIC, AUDIO), (AUDIO, COMIC), (COMIC,), ()],
    ids=["ebook", "ebook+audio", "ebook+comic+audio", "audio+comic", "comic", "no types listed"],
)
def test_no_audiobook_keeps_mixed_catalogs_and_those_that_list_no_types(
    types: tuple[PublicationType, ...],
) -> None:
    """Hadrien: a catalog with ebooks, comics and audiobooks stays. So does one with no types."""
    assert allows(catalog((), types=types), Restrictions(no_audiobook=True))


def test_a_repeated_audiobook_row_still_counts_as_audiobook_only() -> None:
    assert not allows(catalog((), types=(AUDIO, AUDIO)), Restrictions(no_audiobook=True))


def test_no_audiobook_is_ignored_unless_asked_for() -> None:
    assert allows(catalog((), types=(AUDIO,)), NO_RESTRICTIONS)


def test_all_three_restrictions_must_hold() -> None:
    opds_link = (LinkRel.CATALOG, OPDS2)
    wanted = catalog((CatalogKind.PUBLIC,), opds_link, types=(EBOOK, AUDIO))
    audio_only = catalog((CatalogKind.PUBLIC,), opds_link, types=(AUDIO,))
    everything = Restrictions(opds=True, no_open=True, no_audiobook=True)
    assert allows(wanted, everything)
    assert not allows(audio_only, everything)
