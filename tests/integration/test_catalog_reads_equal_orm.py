"""The one-statement reads give exactly what the ORM load gives (ADR-062).

`fetch_recommended_catalogs` and `fetch_catalog_by_id` return fresh transient objects built from
one row each; the importers' `fetch_catalog_by_identity_id` is the unchanged ORM load (attached,
`selectinload`). The behaviour of the public reads must not have changed by a byte, key order
included, so these tests render both and compare the text.

Rollback fixtures: both loads run on the one test session, so they see the same rows.
"""

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import import_catalog_document
from registry.db.models.catalog import Catalog
from registry.domain.catalog_view import CatalogLike
from registry.domain.enums import CatalogStatus
from registry.rendering.catalog_renderer import render_catalog
from registry.repositories.catalog_repository import EAGER_COLLECTIONS, CatalogRepository
from registry.services.feed_service import resolve_top_level_feed
from tests.conftest import LIBRARIES_FILE, SEED_FILE
from tests.read_helpers import link_document
from tests.search_helpers import (
    ALL_TITLES,
    RECOMMENDED_TITLES,
    assert_valid_catalog,
    assert_valid_feed,
)

pytestmark = pytest.mark.integration

BASE = "https://registry.example"
ACCEPT_LANGUAGES = [None, "fr", "en", "fr-BE", "ja", "fr;q=0.5,en"]
KINDS = ["open", "public", "academic", "school", "specialized"]
PUBLICATION_TYPES = ["ebook", "audiobook", "comic", "newspaper", "magazine", "journal", "article"]
LANGUAGES = [f"x{a}{b}" for a in "abcdef" for b in "abcde"]  # 30 distinct lowercase tags
SUBDIVISION_CODES = [f"FR-Q{n:02d}" for n in range(30)]


def as_text(catalog: CatalogLike) -> str:
    """Key order included: compared as text, not as dicts."""
    return json.dumps(render_catalog(catalog, base_url=BASE), ensure_ascii=False)


async def orm_catalog(session: AsyncSession, catalog_id: uuid.UUID) -> Catalog:
    """The importers' ORM load, which is not the code under test."""
    found = await CatalogRepository(session).fetch_catalog_by_identity_id(catalog_id)
    assert found is not None
    return found


async def orm_recommended(session: AsyncSession) -> list[Catalog]:
    """The feed's rows as the ORM loads them, in the order the feed promises."""
    statement = (
        select(Catalog)
        .where(Catalog.recommended, Catalog.status == CatalogStatus.ACTIVE)
        .order_by(Catalog.created_at.desc(), Catalog.title)
        .options(*EAGER_COLLECTIONS)
    )
    return list((await session.scalars(statement)).unique().all())


class OrmReader:
    """A `CatalogReader` over the ORM load, to build the reference feed through the same service."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_recommended_catalogs(self) -> Sequence[Catalog]:
        return await orm_recommended(self._session)

    async def fetch_catalog_by_id(self, catalog_id: uuid.UUID) -> Catalog | None:
        return await CatalogRepository(self._session).fetch_catalog_by_identity_id(catalog_id)


async def add_subdivisions(session: AsyncSession, codes: list[str]) -> None:
    """Reference rows for the test's transaction only (the seeded table holds a handful)."""
    for code in codes:
        await session.execute(
            text("INSERT INTO subdivisions (code, country_alpha2) VALUES (:c, 'FR')"), {"c": code}
        )


async def seed_both(session: AsyncSession) -> None:
    from registry.cli.seed import seed_catalogs  # noqa: PLC0415

    await seed_catalogs(session, SEED_FILE)
    await seed_catalogs(session, LIBRARIES_FILE, recommended=False)
    await session.commit()


# --- B. every catalog of both data files ---------------------------------------------------------


async def test_every_catalog_of_both_data_files_renders_identically_through_both_loads(
    db_session: AsyncSession,
) -> None:
    await seed_both(db_session)
    ids = list((await db_session.scalars(select(Catalog.id))).all())
    assert len(ids) == len(ALL_TITLES) >= 12
    repository = CatalogRepository(db_session)

    for catalog_id in ids:
        one_statement = await repository.fetch_catalog_by_id(catalog_id)
        assert one_statement is not None
        loaded = await orm_catalog(db_session, catalog_id)

        assert as_text(one_statement) == as_text(loaded), loaded.title
        assert_valid_catalog(render_catalog(one_statement, base_url=BASE))


async def test_the_one_statement_read_is_a_fresh_snapshot_not_the_attached_object(
    db_session: AsyncSession,
) -> None:
    await seed_both(db_session)
    catalog_id = (await db_session.scalars(select(Catalog.id).limit(1))).one()
    repository = CatalogRepository(db_session)

    first = await repository.fetch_catalog_by_id(catalog_id)
    second = await repository.fetch_catalog_by_id(catalog_id)
    attached = await orm_catalog(db_session, catalog_id)

    assert first is not None and second is not None
    assert first is not second
    assert first is not attached  # type: ignore[comparison-overlap]
    assert not hasattr(first, "_sa_instance_state"), "a plain value, not an ORM object"
    assert as_text(first) == as_text(second) == as_text(attached)


@pytest.mark.parametrize("accept_language", ACCEPT_LANGUAGES)
async def test_the_whole_feed_is_byte_identical_to_the_one_built_from_the_orm_load(
    db_session: AsyncSession, accept_language: str | None
) -> None:
    await seed_both(db_session)

    new = await resolve_top_level_feed(
        CatalogRepository(db_session), accept_language=accept_language, base_url=BASE
    )
    reference = await resolve_top_level_feed(
        OrmReader(db_session),
        accept_language=accept_language,
        base_url=BASE,
    )

    assert json.dumps(new, ensure_ascii=False) == json.dumps(reference, ensure_ascii=False)
    assert_valid_feed(new)
    assert new["metadata"]["numberOfItems"] == len(new["catalogs"])


async def test_the_feed_without_a_language_header_is_every_recommended_catalog_newest_first(
    db_session: AsyncSession,
) -> None:
    await seed_both(db_session)

    new = await CatalogRepository(db_session).fetch_recommended_catalogs()

    expected = await orm_recommended(db_session)
    assert [c.id for c in new] == [c.id for c in expected]
    created = [c.created_at for c in new]
    assert created == sorted(created, reverse=True)
    assert len(new) == len(RECOMMENDED_TITLES)
    assert {c.title for c in new} == set(RECOMMENDED_TITLES)


# --- C. ordering and filtering -------------------------------------------------------------------


def doc(title: str, **metadata: Any) -> dict[str, Any]:
    return link_document(title, **metadata)


async def test_equal_created_at_is_ordered_by_title(db_session: AsyncSession) -> None:
    at = datetime(2031, 1, 1, tzinfo=UTC)
    for title in ("Charlie", "alpha", "Bravo", "Alpha"):
        await import_catalog_document(db_session, doc(title), recommended=True, ordered_at=at)
    await db_session.flush()

    found = await CatalogRepository(db_session).fetch_recommended_catalogs()

    # The database's collation decides how "alpha" and "Alpha" compare; the ORM reference asks the
    # same database the same question.
    assert [c.title for c in found] == [c.title for c in await orm_recommended(db_session)]
    assert {c.title for c in found} == {"Charlie", "alpha", "Bravo", "Alpha"}
    assert [c.title for c in found].index("Bravo") < [c.title for c in found].index("Charlie")


async def test_a_newer_catalog_precedes_an_older_one_whatever_its_title(
    db_session: AsyncSession,
) -> None:
    epoch = datetime(2031, 1, 1, tzinfo=UTC)
    for n, title in enumerate(("Zulu", "Mike", "Alpha")):
        await import_catalog_document(
            db_session, doc(title), recommended=True, ordered_at=epoch + timedelta(hours=n)
        )
    await db_session.flush()

    found = await CatalogRepository(db_session).fetch_recommended_catalogs()

    assert [c.title for c in found] == ["Alpha", "Mike", "Zulu"]


async def test_only_recommended_and_active_catalogs_are_in_the_feed(
    db_session: AsyncSession,
) -> None:
    kept, _ = await import_catalog_document(db_session, doc("Kept"), recommended=True)
    plain, _ = await import_catalog_document(db_session, doc("Plain"), recommended=False)
    hidden, _ = await import_catalog_document(db_session, doc("Hidden"), recommended=True)
    await db_session.flush()
    await db_session.execute(
        text("UPDATE catalogs SET status = 'suggested', published_at = NULL WHERE id = :i"),
        {"i": hidden.id},
    )

    found = await CatalogRepository(db_session).fetch_recommended_catalogs()

    assert [c.id for c in found] == [kept.id]
    assert plain.id not in {c.id for c in found}


async def test_a_recommended_catalog_set_to_suggested_disappears_on_the_next_read(
    db_session: AsyncSession,
) -> None:
    """The public read is a snapshot: a change goes through the database and is seen by the next
    read, not by an object already returned."""
    catalog, _ = await import_catalog_document(db_session, doc("Fading"), recommended=True)
    await db_session.flush()
    repository = CatalogRepository(db_session)
    before = await repository.fetch_recommended_catalogs()

    await db_session.execute(
        text("UPDATE catalogs SET status = 'suggested', published_at = NULL WHERE id = :i"),
        {"i": catalog.id},
    )
    after = await repository.fetch_recommended_catalogs()

    assert [c.title for c in before] == ["Fading"]  # the snapshot did not change under the caller
    assert after == []
    assert await repository.fetch_catalog_by_id(catalog.id) is None


async def test_an_active_catalog_that_is_not_recommended_is_readable_by_id_but_not_in_the_feed(
    db_session: AsyncSession,
) -> None:
    catalog, _ = await import_catalog_document(db_session, doc("Plain"), recommended=False)
    await db_session.flush()
    repository = CatalogRepository(db_session)

    assert await repository.fetch_recommended_catalogs() == []
    found = await repository.fetch_catalog_by_id(catalog.id)
    assert found is not None and found.title == "Plain"


async def test_a_suggested_catalog_is_hidden_from_the_public_read_but_not_from_the_importer(
    db_session: AsyncSession,
) -> None:
    catalog, _ = await import_catalog_document(db_session, doc("Pending"), recommended=False)
    await db_session.flush()
    catalog_id = catalog.id
    await db_session.execute(
        text("UPDATE catalogs SET status = 'suggested', published_at = NULL WHERE id = :i"),
        {"i": catalog_id},
    )
    db_session.expire_all()
    repository = CatalogRepository(db_session)

    assert await repository.fetch_catalog_by_id(catalog_id) is None
    assert (await repository.fetch_catalog_by_identity_id(catalog_id)) is not None


async def test_the_language_sort_works_on_the_mapped_objects_and_ties_on_created_at(
    db_session: AsyncSession,
) -> None:
    """The service sorts on `created_at`, which the mapper copies for the purpose."""
    epoch = datetime(2031, 1, 1, tzinfo=UTC)
    specs = [
        ("Old french", ["fr"], 0),
        ("New french", ["fr"], 1),
        ("No language", [], 2),
        ("English", ["en"], 3),
    ]
    for title, languages, hours in specs:
        metadata: dict[str, Any] = {"supportedLanguages": languages} if languages else {}
        await import_catalog_document(
            db_session,
            doc(title, **metadata),
            recommended=True,
            ordered_at=epoch + timedelta(hours=hours),
        )
    await db_session.flush()
    repository = CatalogRepository(db_session)

    async def titles(accept_language: str | None) -> list[str]:
        feed = await resolve_top_level_feed(
            repository, accept_language=accept_language, base_url=BASE
        )
        return [c["metadata"]["title"] for c in feed["catalogs"]]

    assert await titles(None) == ["English", "No language", "New french", "Old french"]
    assert await titles("fr") == ["New french", "Old french", "No language"]
    assert await titles("en") == ["English", "No language"]
    assert await titles("ja") == ["No language"]


# --- G. edge catalogs, through both new reads ----------------------------------------------------


def many_links_document(title: str, rel: str, hrefs: list[str]) -> dict[str, Any]:
    return doc(title, rel_extra=[{"href": href, "rel": rel} for href in hrefs])


def everything_document() -> dict[str, Any]:
    links = [
        {"href": f"https://full.example/{rel}/{n}", "rel": rel, "type": "text/html"}
        for rel in ("shelf", "icon", "authenticate", "alternate", "profile", "search")
        for n in range(6)
    ]
    for link in links:
        if link["rel"] == "search":
            link["templated"] = True  # type: ignore[assignment]
        if link["rel"] == "authenticate":
            link["title"] = f"Sign in {link['href']}"
    return doc(
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
        rel_extra=links,
    )


def unicode_document() -> dict[str, Any]:
    return doc(
        'Bibliothèque "Ørsted" \u2013 ß Ł 日本語 <b>&amp;</b> \\ back',
        description='Ligne 1\nLigne "2" emoji \U0001f4da',
        city="Zürich",
        rel_extra=[
            {
                "href": "https://example.org/ü/ö?q=a%20b&r='x'#frag",
                "rel": "alternate",
                "title": 'Titre "cité" 日本語',
            }
        ],
    )


EDGE_DOCUMENTS: dict[str, Callable[[], dict[str, Any]]] = {
    "no-links": lambda: doc("Linkless library"),
    "forty-links": lambda: many_links_document(
        "Forty library", "alternate", [f"https://forty.example/alt/{n:02d}" for n in range(39)]
    ),
    "nulls": lambda: doc("Nullish library"),
    "everything": everything_document,
    "unicode": unicode_document,
    # Inserted in REVERSE href order, with index scans off below.
    "shared-rel": lambda: many_links_document(
        "Order library",
        "alternate",
        [f"https://order.example/alt/{n:02d}" for n in reversed(range(39))],
    ),
    "twins": lambda: doc("Twin gamma", kind=KINDS, supportedLanguages=["fr", "en"]),
}


async def build_edge_catalog(session: AsyncSession, name: str) -> uuid.UUID:
    """One catalog of the named shape, recommended so the feed carries it too."""
    if name == "everything":
        await add_subdivisions(session, SUBDIVISION_CODES)
    if name == "twins":
        for twin in ("alpha", "beta"):
            await import_catalog_document(
                session,
                doc(f"Twin {twin}", kind=KINDS, supportedLanguages=["fr", "en"]),
                recommended=True,
            )
    catalog, _ = await import_catalog_document(session, EDGE_DOCUMENTS[name](), recommended=True)
    await session.flush()
    if name == "no-links":
        await session.execute(text("DELETE FROM links WHERE catalog_id = :c"), {"c": catalog.id})
    if name == "shared-rel":
        for setting in ("enable_indexscan", "enable_bitmapscan", "enable_indexonlyscan"):
            await session.execute(text(f"SET LOCAL {setting} = off"))
    catalog_id = catalog.id
    session.expire_all()
    return catalog_id


EDGE_NAMES = ["no-links", "forty-links", "nulls", "everything", "unicode", "shared-rel", "twins"]


@pytest.mark.parametrize("name", EDGE_NAMES)
async def test_an_edge_catalog_renders_like_the_orm_through_by_id_and_through_the_feed(
    db_session: AsyncSession, name: str
) -> None:
    catalog_id = await build_edge_catalog(db_session, name)
    repository = CatalogRepository(db_session)

    by_id = await repository.fetch_catalog_by_id(catalog_id)
    in_feed = next(c for c in await repository.fetch_recommended_catalogs() if c.id == catalog_id)
    loaded = await orm_catalog(db_session, catalog_id)

    assert by_id is not None
    assert as_text(by_id) == as_text(in_feed) == as_text(loaded)
    assert_valid_catalog(render_catalog(by_id, base_url=BASE))


async def test_a_catalog_without_links_has_an_empty_list_and_only_its_self_link(
    db_session: AsyncSession,
) -> None:
    catalog_id = await build_edge_catalog(db_session, "no-links")
    repository = CatalogRepository(db_session)

    for catalog in (
        await repository.fetch_catalog_by_id(catalog_id),
        next(c for c in await repository.fetch_recommended_catalogs() if c.id == catalog_id),
    ):
        assert catalog is not None
        assert catalog.links == []
        assert [i["rel"] for i in render_catalog(catalog, base_url=BASE)["links"]] == ["self"]


async def test_forty_links_arrive_once_each(db_session: AsyncSession) -> None:
    catalog_id = await build_edge_catalog(db_session, "forty-links")

    found = await CatalogRepository(db_session).fetch_catalog_by_id(catalog_id)

    assert found is not None
    hrefs = [link.href for link in found.links]
    assert len(hrefs) == len(set(hrefs)) == 40


async def test_null_description_city_and_coverage_are_omitted_not_rendered_as_null(
    db_session: AsyncSession,
) -> None:
    catalog_id = await build_edge_catalog(db_session, "nulls")
    repository = CatalogRepository(db_session)

    for catalog in (
        await repository.fetch_catalog_by_id(catalog_id),
        next(c for c in await repository.fetch_recommended_catalogs() if c.id == catalog_id),
    ):
        assert catalog is not None
        assert (catalog.city, catalog.description, catalog.coverage) == (None, None, None)
        metadata = render_catalog(catalog, base_url=BASE)["metadata"]
        for key in ("city", "description", "coverage", "country", "subdivisions"):
            assert key not in metadata


async def test_children_are_not_multiplied_by_each_other(db_session: AsyncSession) -> None:
    """Five children of 5, 7, 30, 30 and 36 elements: a join across them would give a million
    rows. Every collection holds each of its values once."""
    catalog_id = await build_edge_catalog(db_session, "everything")
    repository = CatalogRepository(db_session)

    for catalog in (
        await repository.fetch_catalog_by_id(catalog_id),
        next(c for c in await repository.fetch_recommended_catalogs() if c.id == catalog_id),
    ):
        assert catalog is not None
        assert sorted(row.kind.value for row in catalog.kinds) == sorted(KINDS)
        assert len(catalog.publication_types) == len(PUBLICATION_TYPES)
        assert sorted(row.language_tag for row in catalog.languages) == sorted(LANGUAGES)
        assert sorted(row.subdivision_code for row in catalog.subdivisions) == SUBDIVISION_CODES
        assert len(catalog.links) == 37  # 36 + the identity link
        assert len({link.href for link in catalog.links}) == 37


async def test_one_catalog_appears_once_in_the_feed_whatever_its_children(
    db_session: AsyncSession,
) -> None:
    catalog_id = await build_edge_catalog(db_session, "everything")

    found = await CatalogRepository(db_session).fetch_recommended_catalogs()

    assert [c.id for c in found].count(catalog_id) == 1
    assert len(found) == 1


async def test_twin_catalogs_keep_their_own_children(db_session: AsyncSession) -> None:
    await build_edge_catalog(db_session, "twins")

    found = await CatalogRepository(db_session).fetch_recommended_catalogs()

    assert len(found) == 3
    for catalog in found:
        assert sorted(row.language_tag for row in catalog.languages) == ["en", "fr"]
        assert len(catalog.kinds) == 5
        assert len(catalog.links) == 1


async def test_links_sharing_a_rel_render_in_href_order_through_both_reads(
    db_session: AsyncSession,
) -> None:
    """The links were inserted in REVERSE href order and index scans are off, so heap order
    (descending) is the only order the database can hand back; the renderer's sort is what makes
    the document the same by every path."""
    catalog_id = await build_edge_catalog(db_session, "shared-rel")
    repository = CatalogRepository(db_session)

    by_id = await repository.fetch_catalog_by_id(catalog_id)
    in_feed = next(c for c in await repository.fetch_recommended_catalogs() if c.id == catalog_id)

    for catalog in (by_id, in_feed):
        assert catalog is not None
        rendered = render_catalog(catalog, base_url=BASE)["links"]
        alternates = [i["href"] for i in rendered if i["rel"] == "alternate"]
        assert len(alternates) == 39
        assert alternates == sorted(alternates)


async def test_unicode_survives_untouched_through_both_reads(db_session: AsyncSession) -> None:
    catalog_id = await build_edge_catalog(db_session, "unicode")
    repository = CatalogRepository(db_session)

    for catalog in (
        await repository.fetch_catalog_by_id(catalog_id),
        next(c for c in await repository.fetch_recommended_catalogs() if c.id == catalog_id),
    ):
        assert catalog is not None
        rendered = render_catalog(catalog, base_url=BASE)
        assert rendered["metadata"]["title"].startswith('Bibliothèque "Ørsted"')
        assert rendered["metadata"]["city"] == "Zürich"
        alternate = next(i for i in rendered["links"] if i["rel"] == "alternate")
        assert alternate["href"] == "https://example.org/ü/ö?q=a%20b&r='x'#frag"
        assert alternate["title"] == 'Titre "cité" 日本語'
