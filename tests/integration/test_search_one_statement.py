"""The one search statement returns every child once, complete, and renders like the ORM does.

Search answers a page and every child of every catalog on it (kinds, publication types,
languages, subdivisions, links) in one statement (ADR-060). The danger of that shape is in the
details a per-collection query cannot get wrong: a join that multiplies rows, an array that
drops an element, a JSON aggregate that loses a field, a page that is cut in the wrong place.
These tests put catalogs with no children, with the maximum of every child, and with nulls
through the repository and compare the result with what the ORM loads for the same catalog.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import import_catalog_document
from registry.db.models.catalog import Catalog
from registry.domain.catalog_view import CatalogLike
from registry.domain.search_query import parse_search_query
from registry.rendering.catalog_renderer import render_catalog
from registry.repositories.catalog_repository import CatalogRepository
from registry.repositories.search_repository import CatalogSearchRepository
from tests.search_helpers import ALL_TITLES

pytestmark = pytest.mark.integration

BASE = "https://registry.example"
KINDS = ["open", "public", "academic", "school", "specialized"]
PUBLICATION_TYPES = ["ebook", "audiobook", "comic", "newspaper", "magazine", "journal", "article"]
LINK_RELS = ["catalog", "shelf", "icon", "authenticate", "alternate", "profile", "search"]
LANGUAGES = [f"x{a}{b}" for a in "abcdef" for b in "abcde"]  # 30 distinct lowercase tags
SUBDIVISION_CODES = [f"FR-Q{n:02d}" for n in range(30)]


def rendered_json(catalog: CatalogLike) -> str:
    """Key order included: compared as text, not as dicts."""
    return json.dumps(render_catalog(catalog, base_url=BASE), ensure_ascii=False)


def rendered_up_to_link_order(catalog: CatalogLike) -> str:
    """Like `rendered_json`, but links that share a rel are put in href order first. The renderer
    now orders them itself (`order_links(..., then_by=href)`), so this is belt and braces for the
    tests about something else; the one about order is the last in the file."""
    document = render_catalog(catalog, base_url=BASE)
    document["links"] = sorted(document["links"], key=lambda link: (link["rel"], link["href"]))
    return json.dumps(document, ensure_ascii=False)


async def search(
    session: AsyncSession, query: str, *, limit: int = 50, offset: int = 0
) -> tuple[int, list[CatalogLike]]:
    page = await CatalogSearchRepository(session).search_catalogs(
        parse_search_query(query), limit=limit, offset=offset
    )
    return page.total, list(page.catalogs)


async def orm_catalog(session: AsyncSession, catalog_id: uuid.UUID) -> Catalog:
    """The ORM load (`selectinload`), the importers' lookup. Not `load_catalog_by_id`: that is
    itself one statement now, and comparing two of the same would prove nothing."""
    found = await CatalogRepository(session).fetch_catalog_by_identity_id(catalog_id)
    assert found is not None
    return found


def document(title: str, **metadata: Any) -> dict[str, Any]:
    slug = uuid.uuid4().hex
    links = metadata.pop("links", None)
    return {
        "metadata": {"title": title, "kind": ["public"], **metadata},
        "links": (
            links
            if links is not None
            else [
                {
                    "href": f"https://{slug}.example/opds",
                    "type": "application/opds+json",
                    "rel": "catalog",
                }
            ]
        ),
    }


async def add_subdivisions(session: AsyncSession, codes: list[str]) -> None:
    """Reference rows for the test's transaction only (the seeded table holds a handful)."""
    for code in codes:
        await session.execute(
            text("INSERT INTO subdivisions (code, country_alpha2) VALUES (:c, 'FR')"), {"c": code}
        )


# --- A. the mapped object renders exactly like the ORM-loaded one --------------------------------


async def test_every_seeded_catalog_renders_identically_through_search_and_through_the_orm(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    assert len(ALL_TITLES) >= 12
    for title in ALL_TITLES:
        _total, found = await search(db_session, title)
        mapped = next(c for c in found if c.title == title)
        loaded = await orm_catalog(db_session, mapped.id)

        assert rendered_json(mapped) == rendered_json(loaded), title


async def test_a_catalog_with_every_child_at_its_maximum_renders_like_the_orm(
    db_session: AsyncSession,
) -> None:
    await add_subdivisions(db_session, SUBDIVISION_CODES)
    links = [
        {"href": f"https://full.example/{rel}/{n}", "rel": rel, "type": "text/html"}
        for rel in LINK_RELS
        for n in range(6)
    ]
    for link in links:
        if link["rel"] == "search":
            link["templated"] = True  # type: ignore[assignment]
        if link["rel"] == "authenticate":
            link["title"] = f"Sign in {link['href']}"
    catalog, _ = await import_catalog_document(
        db_session,
        document(
            "Fullhouse library",
            kind=KINDS,
            publicationTypes=PUBLICATION_TYPES,
            supportedLanguages=LANGUAGES,
            subdivisions=SUBDIVISION_CODES,
            country="FR",
            city="Paris",
            description="A very full catalog",
            coverage="subdivisions",
            color="pink",
            links=links,
        ),
        recommended=False,
    )
    await db_session.flush()

    total, found = await search(db_session, "fullhouse")
    mapped = found[0]
    loaded = await orm_catalog(db_session, catalog.id)

    assert total == 1
    assert rendered_up_to_link_order(mapped) == rendered_up_to_link_order(loaded)
    assert sorted(row.kind.value for row in mapped.kinds) == sorted(KINDS)
    assert len(mapped.publication_types) == 7
    assert len(mapped.languages) == len(set(LANGUAGES)) == 30
    assert len(mapped.subdivisions) == len(set(SUBDIVISION_CODES)) == 30
    assert len(mapped.links) == 42


# --- B. one row per catalog, every child exactly once ---------------------------------------------


async def test_a_catalog_with_no_links_comes_back_with_an_empty_link_list(
    db_session: AsyncSession,
) -> None:
    """Links are aggregated, not joined: a catalog without any is `[]`, not a dropped row."""
    imported, _ = await import_catalog_document(
        db_session,
        document("Linkless library"),
        recommended=False,
    )
    await db_session.flush()
    await db_session.execute(text("DELETE FROM links WHERE catalog_id = :c"), {"c": imported.id})

    total, found = await search(db_session, "linkless")

    assert total == 1
    assert found[0].links == []
    assert [item["rel"] for item in render_catalog(found[0], base_url=BASE)["links"]] == ["self"]


async def test_forty_links_arrive_once_each_and_render_like_the_orm(
    db_session: AsyncSession,
) -> None:
    links = [{"href": "https://forty.example/catalog", "rel": "catalog"}]
    links += [{"href": f"https://forty.example/alt/{n:02d}", "rel": "alternate"} for n in range(39)]
    imported, _ = await import_catalog_document(
        db_session, document("Forty library", links=links), recommended=False
    )
    await db_session.flush()

    _total, found = await search(db_session, "forty")

    hrefs = [link.href for link in found[0].links]
    assert len(hrefs) == 40
    assert len(set(hrefs)) == 40
    loaded = await orm_catalog(db_session, imported.id)
    assert rendered_up_to_link_order(found[0]) == rendered_up_to_link_order(loaded)


async def test_null_city_description_and_coverage_are_omitted_not_rendered_as_null(
    db_session: AsyncSession,
) -> None:
    await import_catalog_document(db_session, document("Nullish library"), recommended=False)
    await db_session.flush()

    _total, found = await search(db_session, "nullish")
    metadata = render_catalog(found[0], base_url=BASE)["metadata"]

    assert (found[0].city, found[0].description, found[0].coverage) == (None, None, None)
    for key in ("city", "description", "coverage", "country", "subdivisions"):
        assert key not in metadata


async def test_large_child_collections_do_not_multiply_rows_or_totals(
    db_session: AsyncSession,
) -> None:
    """Five children of 5, 7, 30, 30 and 42 elements: a join across them would give
    5 x 7 x 30 x 30 x 42 rows. The page must hold one catalog and the total must be 1."""
    await add_subdivisions(db_session, SUBDIVISION_CODES)
    await import_catalog_document(
        db_session,
        document(
            "Multiplier library",
            kind=KINDS,
            publicationTypes=PUBLICATION_TYPES,
            supportedLanguages=LANGUAGES,
            subdivisions=SUBDIVISION_CODES,
            links=[{"href": f"https://mult.example/{n}", "rel": "alternate"} for n in range(41)]
            + [{"href": "https://mult.example/cat", "rel": "catalog"}],
        ),
        recommended=False,
    )
    await db_session.flush()

    total, found = await search(db_session, "multiplier")

    assert (total, len(found)) == (1, 1)
    child_counts = (len(found[0].kinds), len(found[0].publication_types), len(found[0].languages))
    assert child_counts == (5, 7, 30)
    assert len(found[0].subdivisions) == 30
    assert len(found[0].links) == 42


async def test_each_array_holds_distinct_values_when_two_catalogs_share_them(
    db_session: AsyncSession,
) -> None:
    for name in ("alpha", "beta"):
        await import_catalog_document(
            db_session,
            document(f"Twin {name}", kind=KINDS, supportedLanguages=["fr", "en"]),
            recommended=False,
        )
    await db_session.flush()

    total, found = await search(db_session, "twin")

    assert total == 2
    for catalog in found:
        assert sorted(row.language_tag for row in catalog.languages) == ["en", "fr"]
        assert len(catalog.kinds) == 5
        assert len(catalog.links) == 1


@pytest.fixture
async def ordered_pages(db_session: AsyncSession) -> list[str]:
    """25 catalogs matching "paging", newest first, in the order search must return them."""
    epoch = datetime(2031, 1, 1, tzinfo=UTC)
    titles: list[str] = []
    for n in range(25):
        title = f"Paging shelf {n:02d}"
        await import_catalog_document(
            db_session, document(title), recommended=False, ordered_at=epoch + timedelta(hours=n)
        )
        titles.append(title)
    await db_session.flush()
    return titles[::-1]  # same score everywhere, so newest `created_at` first


@pytest.mark.parametrize(
    ("limit", "offset"), [(10, 0), (10, 10), (10, 20), (7, 21), (1, 24), (25, 0), (50, 0)]
)
async def test_a_page_is_the_right_slice_of_the_ranked_list_with_the_full_total(
    db_session: AsyncSession, ordered_pages: list[str], limit: int, offset: int
) -> None:
    total, found = await search(db_session, "paging", limit=limit, offset=offset)

    assert total == 25
    assert [c.title for c in found] == ordered_pages[offset : offset + limit]


async def test_pages_laid_end_to_end_are_the_whole_list_with_no_repeat_and_no_gap(
    db_session: AsyncSession, ordered_pages: list[str]
) -> None:
    seen: list[str] = []
    for offset in range(0, 30, 10):
        _total, found = await search(db_session, "paging", limit=10, offset=offset)
        seen += [c.title for c in found]

    assert seen == ordered_pages


async def test_a_page_past_the_end_is_empty_and_still_reports_the_total(
    db_session: AsyncSession, ordered_pages: list[str]
) -> None:
    total, found = await search(db_session, "paging", limit=10, offset=25)
    far_total, far_found = await search(db_session, "paging", limit=10, offset=5000)

    assert (total, found) == (25, [])
    assert (far_total, far_found) == (25, [])


async def test_nothing_matching_gives_total_zero_and_no_catalogs(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    assert await search(db_session, "xyzzy") == (0, [])
    assert await search(db_session, "xyzzy", limit=1, offset=3) == (0, [])


@pytest.mark.parametrize(
    "planner",
    [
        "SET LOCAL enable_nestloop = off",
        "SET LOCAL enable_nestloop = off; SET LOCAL enable_hashjoin = off",
        "SET LOCAL enable_nestloop = off; SET LOCAL enable_mergejoin = off",
        "SET LOCAL enable_material = off; SET LOCAL enable_hashjoin = off",
    ],
)
async def test_the_page_order_does_not_depend_on_the_join_strategy_the_planner_picks(
    db_session: AsyncSession, ordered_pages: list[str], planner: str
) -> None:
    """The final `ORDER BY p.rn` is what guarantees the order of the returned rows; without it
    the order is whatever the join over the page happens to emit."""
    for statement in planner.split("; "):
        await db_session.execute(text(statement))

    total, found = await search(db_session, "paging", limit=25)

    assert total == 25
    assert [c.title for c in found] == ordered_pages


async def test_links_sharing_a_rel_render_in_the_same_order_through_search_and_the_orm(
    db_session: AsyncSession,
) -> None:
    """Regression: the links aggregate in the search statement, like the ORM load, returns links
    that share a rel in no promised order: whatever the scan finds. The renderer now sorts them by
    href, so the same catalog gives the same document by every path and every plan.

    Made deterministic instead of lucky: the links are inserted in REVERSE href order and index
    scans are switched off for this transaction, so heap order (descending) is the only order the
    database can hand back. Without the sort in `order_links` the two paths would show descending
    hrefs here; the unit tests for `order_links` pin the rule itself."""
    links = [{"href": "https://order.example/catalog", "rel": "catalog"}]
    links += [
        {"href": f"https://order.example/alt/{n:02d}", "rel": "alternate"}
        for n in reversed(range(39))
    ]
    imported, _ = await import_catalog_document(
        db_session, document("Order library", links=links), recommended=False
    )
    await db_session.flush()
    catalog_id = imported.id
    for setting in ("enable_indexscan", "enable_bitmapscan", "enable_indexonlyscan"):
        await db_session.execute(text(f"SET LOCAL {setting} = off"))
    db_session.expire_all()

    _total, found = await search(db_session, "order")
    loaded = await orm_catalog(db_session, catalog_id)

    searched = [link["href"] for link in render_catalog(found[0], base_url=BASE)["links"]]
    alternates = [href for href in searched if "/alt/" in href]
    assert alternates == sorted(alternates), "search must list links of one rel in href order"
    assert rendered_json(found[0]) == rendered_json(loaded)
