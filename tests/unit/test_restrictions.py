"""`restrict`: parsing, the link form, and the two rules on plain values."""

import re
import uuid
from datetime import UTC, datetime

import pytest

from registry.domain.catalog_view import CatalogView, KindRow, LinkRow
from registry.domain.enums import CatalogColor, CatalogKind, LinkRel
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


def catalog(kinds: tuple[CatalogKind, ...], *links: tuple[LinkRel, str | None]) -> CatalogView:
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
        publication_types=[],
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
    ],
)
def test_parse(value: str | None, expected: Restrictions) -> None:
    assert parse_restrictions(value) == expected


@pytest.mark.parametrize("value", ["", "opds", "no-open", "opds,no-open", "opds,opds"])
def test_the_route_pattern_accepts(value: str) -> None:
    assert re.fullmatch(RESTRICT_PATTERN, value)


@pytest.mark.parametrize("value", ["opsd", "OPDS", "opds,", ",opds", "opds, no-open", "all"])
def test_the_route_pattern_rejects(value: str) -> None:
    assert not re.fullmatch(RESTRICT_PATTERN, value)


def test_query_value_is_canonical_and_none_when_unrestricted() -> None:
    assert NO_RESTRICTIONS.query_value is None
    assert Restrictions(no_open=True, opds=True).query_value == "opds,no-open"


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
