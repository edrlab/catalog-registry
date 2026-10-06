"""`build_catalog_from_row`: a row of `CATALOG_COLUMNS` becomes the `Catalog` the renderer reads
(ADR-060, ADR-062). Search, the feed and one catalog all map their rows with it.

No database: a row is any mapping, and asyncpg hands `jsonb` back as text, so both forms of the
`links` column are exercised. The integration twins of this file (`test_search_one_statement`,
`test_catalog_reads_equal_orm`) prove the mapped object renders exactly like the ORM-loaded one.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import inspect

from registry.db.models.catalog import Catalog
from registry.domain.enums import (
    CatalogColor,
    CatalogKind,
    CoverageScope,
    LinkRel,
    PublicationType,
)
from registry.domain.links import order_links
from registry.rendering.catalog_renderer import render_catalog
from registry.repositories import catalog_rows, search_repository
from registry.repositories.catalog_rows import build_catalog_from_row

pytestmark = pytest.mark.unit

BASE = "https://registry.example"
ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
CREATED_AT = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)


def link(
    href: str = "https://example.org/opds",
    rel: str = "catalog",
    media_type: str | None = "application/opds+json",
    templated: bool | None = False,
    title: str | None = None,
) -> dict[str, Any]:
    return {"href": href, "type": media_type, "rel": rel, "templated": templated, "title": title}


def make_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "total": 1,
        "tier": 1,
        "score": 0.5,
        "catalog_id": ID,
        "created_at": CREATED_AT,
        "title": "Library",
        "description": None,
        "color": "gray",
        "country_code": None,
        "city": None,
        "coverage": None,
        "kinds": ["public"],
        "publication_types": [],
        "languages": [],
        "subdivisions": [],
        "links": [link()],
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("color", list(CatalogColor))
def test_every_color_converts(color: CatalogColor) -> None:
    assert build_catalog_from_row(make_row(color=color.value)).color is color


@pytest.mark.parametrize("coverage", list(CoverageScope))
def test_every_coverage_converts(coverage: CoverageScope) -> None:
    assert build_catalog_from_row(make_row(coverage=coverage.value)).coverage is coverage


def test_a_missing_coverage_stays_none_and_is_not_rendered() -> None:
    catalog = build_catalog_from_row(make_row(coverage=None))

    assert catalog.coverage is None
    assert "coverage" not in render_catalog(catalog, base_url=BASE)["metadata"]


def test_an_empty_string_coverage_is_none_too() -> None:
    assert build_catalog_from_row(make_row(coverage="")).coverage is None


def test_every_kind_converts() -> None:
    catalog = build_catalog_from_row(make_row(kinds=[k.value for k in CatalogKind]))

    assert [row.kind for row in catalog.kinds] == list(CatalogKind)


def test_every_publication_type_converts() -> None:
    values = [p.value for p in PublicationType]
    catalog = build_catalog_from_row(make_row(publication_types=values))

    assert [row.publication_type for row in catalog.publication_types] == list(PublicationType)


@pytest.mark.parametrize("rel", list(LinkRel))
def test_every_link_rel_converts(rel: LinkRel) -> None:
    catalog = build_catalog_from_row(make_row(links=[link(rel=rel.value)]))

    assert [mapped.rel for mapped in catalog.links] == [rel]


def test_an_unknown_enum_value_fails_loudly_rather_than_rendering_garbage() -> None:
    with pytest.raises(ValueError, match="nope"):
        build_catalog_from_row(make_row(color="nope"))
    with pytest.raises(ValueError, match="nope"):
        build_catalog_from_row(make_row(links=[link(rel="nope")]))


def test_links_as_a_json_string_and_as_a_list_give_the_same_catalog() -> None:
    links = [link(), link("https://example.org/s{?q}", "search", "text/html", True, "Search")]

    from_text = build_catalog_from_row(make_row(links=json.dumps(links)))
    from_list = build_catalog_from_row(make_row(links=links))

    assert render_catalog(from_text, base_url=BASE) == render_catalog(from_list, base_url=BASE)
    assert len(from_text.links) == 2


@pytest.mark.parametrize("empty", [[], "[]"])
def test_empty_links_give_a_catalog_with_only_its_self_link(empty: Any) -> None:
    catalog = build_catalog_from_row(make_row(links=empty))

    assert catalog.links == []
    assert [item["rel"] for item in render_catalog(catalog, base_url=BASE)["links"]] == ["self"]


def test_empty_arrays_give_empty_collections_and_no_optional_keys() -> None:
    catalog = build_catalog_from_row(make_row(kinds=[]))
    metadata = render_catalog(catalog, base_url=BASE)["metadata"]

    assert (catalog.kinds, catalog.languages, catalog.subdivisions, catalog.publication_types) == (
        [],
        [],
        [],
        [],
    )
    assert metadata["kind"] == []
    for key in ("supportedLanguages", "publicationTypes", "subdivisions", "country", "city"):
        assert key not in metadata


@pytest.mark.parametrize(("templated", "expected"), [(True, True), (False, False), (None, False)])
def test_templated_is_a_boolean_whatever_the_json_held(
    templated: bool | None, expected: bool
) -> None:
    catalog = build_catalog_from_row(make_row(links=[link(rel="search", templated=templated)]))
    rendered = render_catalog(catalog, base_url=BASE)["links"][1]

    assert catalog.links[0].templated is expected
    assert ("templated" in rendered) is expected


def test_a_link_without_media_type_or_title_omits_both_keys() -> None:
    catalog = build_catalog_from_row(make_row(links=[link(media_type=None, title=None)]))

    assert render_catalog(catalog, base_url=BASE)["links"][1] == {
        "href": "https://example.org/opds",
        "rel": "catalog",
    }


def test_unicode_quotes_and_markup_survive_untouched() -> None:
    title = "Bibliothèque \"Ørsted\" \u2013 ß Ł 日本語 'single' <b>&amp;</b> \\ back"
    description = 'Ligne 1\nLigne "2" \u2028 emoji \U0001f4da'
    href = "https://example.org/ü/ö?q=a%20b&r='x'#frag"
    links_json = json.dumps([link(href, title='Titre "cité"')], ensure_ascii=False)

    for links in (links_json, json.loads(links_json)):
        catalog = build_catalog_from_row(
            make_row(title=title, description=description, links=links)
        )
        rendered = render_catalog(catalog, base_url=BASE)

        assert rendered["metadata"]["title"] == title
        assert rendered["metadata"]["description"] == description
        assert rendered["links"][1]["href"] == href
        assert rendered["links"][1]["title"] == 'Titre "cité"'


def test_a_very_long_description_is_kept_whole() -> None:
    description = "mot " * 50_000

    catalog = build_catalog_from_row(make_row(description=description))

    assert render_catalog(catalog, base_url=BASE)["metadata"]["description"] == description


def test_many_links_all_arrive_and_render_in_the_deterministic_order() -> None:
    links = [link(f"https://example.org/{n}", "alternate") for n in range(500)]
    links += [link("https://example.org/icon", "icon", "image/png")]

    catalog = build_catalog_from_row(make_row(links=json.dumps(links)))
    rendered = render_catalog(catalog, base_url=BASE)["links"]

    assert len(catalog.links) == 501
    assert len(rendered) == 502
    assert rendered[0]["rel"] == "self"
    expected = [
        item.href
        for item in order_links(
            catalog.links, rel_of=lambda item: item.rel, then_by=lambda item: item.href
        )
    ]
    assert [item["href"] for item in rendered[1:]] == expected
    # Links that share a rel are in plain string order of the href ("10" before "2"), whatever
    # order the database handed them over in.
    alternates = [item["href"] for item in rendered if item["rel"] == "alternate"]
    assert alternates == sorted(alternates)


def test_scalars_and_children_are_mapped_field_by_field() -> None:
    catalog = build_catalog_from_row(
        make_row(
            description="d",
            color="purple",
            country_code="FR",
            city="Paris",
            coverage="local",
            kinds=["open", "school"],
            publication_types=["comic"],
            languages=["fr", "en"],
            subdivisions=["FR-75"],
        )
    )
    metadata = render_catalog(catalog, base_url=BASE)["metadata"]

    assert metadata == {
        "title": "Library",
        "identifier": f"urn:uuid:{ID}",
        "kind": ["open", "school"],
        "description": "d",
        "color": "purple",
        "supportedLanguages": ["en", "fr"],
        "publicationTypes": ["comic"],
        "country": "FR",
        "subdivisions": ["FR-75"],
        "city": "Paris",
        "coverage": "local",
    }


def test_the_mapped_object_is_transient_and_attached_to_no_session() -> None:
    catalog = build_catalog_from_row(make_row(links=[link()], languages=["fr"]))

    assert isinstance(catalog, Catalog)
    state = inspect(catalog)
    assert state.transient
    assert state.session is None
    assert all(inspect(child).session is None for child in (*catalog.links, *catalog.languages))


def test_created_at_is_set_because_the_feed_sorts_on_it_but_is_never_rendered() -> None:
    catalog = build_catalog_from_row(make_row())

    assert catalog.created_at == CREATED_AT
    document = render_catalog(catalog, base_url=BASE)
    assert "created_at" not in document["metadata"]
    assert str(CREATED_AT.year) not in json.dumps(document)


def test_internal_columns_are_not_set_on_the_mapped_object() -> None:
    """R3: only what the renderer projects, and `created_at` for the feed's order, is copied;
    nothing else internal rides along."""
    catalog = build_catalog_from_row(make_row())

    assert catalog.submitter_email is None
    assert catalog.submitter_name is None
    assert catalog.recommended is None
    assert catalog.status is None
    assert catalog.updated_at is None
    assert catalog.published_at is None


def test_an_extra_column_in_the_row_does_not_reach_the_object() -> None:
    """A new column added to the SELECT list cannot leak by accident: the mapper copies named
    fields, not the row's keys (R3)."""
    catalog = build_catalog_from_row(
        make_row(status="suggested", recommended=True, submitter_email="a@b.example")
    )

    assert catalog.status is None
    assert catalog.recommended is None
    assert catalog.submitter_email is None


def test_the_mapper_is_importable_from_the_search_repository_too() -> None:
    """Compatibility: search used to own the mapper."""
    exported = vars(search_repository)
    assert exported["build_catalog_from_row"] is catalog_rows.build_catalog_from_row
    assert exported["fetch_rows"] is catalog_rows.fetch_rows


def test_the_select_list_names_created_at_and_no_internal_column() -> None:
    columns = catalog_rows.CATALOG_COLUMNS
    assert "c.created_at" in columns
    for internal in ("status", "recommended", "submitter", "updated_at", "published_at"):
        assert internal not in columns
