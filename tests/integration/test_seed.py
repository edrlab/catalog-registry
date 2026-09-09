"""The seed path against real Postgres. `conventions/testing.md` §5.6."""

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import import_catalog_document, resolve_identity_href, seed_catalogs
from registry.core.errors import ValidationError
from registry.core.schema_validation import build_schema_validator
from registry.db.models.catalog import Catalog, CatalogLanguageRow
from registry.db.models.link import Link
from registry.domain.enums import CoverageScope
from tests.conftest import SEED_CATALOG_COUNT, SEED_FILE

pytestmark = pytest.mark.integration


async def count(session: AsyncSession, model: type) -> int:
    return (await session.scalar(select(func.count()).select_from(model))) or 0


async def test_seed_imports_every_recommended_catalog(db_session: AsyncSession) -> None:
    """ADR-029 — presence in the file is the recommended flag."""
    created, updated = await seed_catalogs(db_session, SEED_FILE)
    await db_session.commit()

    assert (created, updated) == (SEED_CATALOG_COUNT, 0)
    assert await count(db_session, Catalog) == SEED_CATALOG_COUNT


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    """Running it twice produces the same database state. Tested by running it twice."""
    await seed_catalogs(db_session, SEED_FILE)
    await db_session.commit()
    before = (await count(db_session, Catalog), await count(db_session, Link))

    created, updated = await seed_catalogs(db_session, SEED_FILE)
    await db_session.commit()

    assert (created, updated) == (0, SEED_CATALOG_COUNT)
    assert (await count(db_session, Catalog), await count(db_session, Link)) == before


async def test_language_tags_are_lowercased_on_ingest(db_session: AsyncSession) -> None:
    """R2 — tags land lowercase or not at all.

    The input is written uppercase here rather than read from the seed file. The file itself
    is lowercase now, so seeding it would assert nothing: this test has to supply the casing
    it is defending against. BCP-47 declares tags case-insensitive and real producers do send
    `EN`, so the ingest path must fold them whatever the repository's own fixtures look like.
    """
    document = {
        "metadata": {
            "title": "Uppercase Tags",
            "kind": ["open"],
            "supportedLanguages": ["EN", "Fr-BE"],
        },
        "links": [{"href": "https://example.org/opds", "rel": "catalog"}],
    }
    await import_catalog_document(db_session, document, recommended=True)
    await db_session.commit()

    tags = (await db_session.scalars(select(CatalogLanguageRow.language_tag))).all()
    assert sorted(tags) == ["en", "fr-be"]


async def test_the_seed_file_ships_lowercase_language_tags() -> None:
    """Separate from the ingest guard above, and deliberately so.

    Lowercasing on ingest is the rule that must hold for any input (R2). This asserts the
    repository's own fixtures are already canonical, so a diff never turns on casing.
    """
    for catalog in json.loads(SEED_FILE.read_text(encoding="utf-8"))["catalogs"]:
        tags = catalog["metadata"].get("supportedLanguages", [])
        assert tags == [tag.lower() for tag in tags], catalog["metadata"]["title"]


async def test_undeclared_coverage_is_stored_as_null(db_session: AsyncSession) -> None:
    """ADR-032 — a catalog that did not declare coverage gets NULL, never `global`.

    Lirtuel declares `subdivisions`, so this asserts the two cases are distinguished rather
    than that every row is NULL — a blanket assertion would have passed even if the column
    were never populated at all.
    """
    await seed_catalogs(db_session, SEED_FILE)
    await db_session.commit()

    rows = (await db_session.execute(select(Catalog.title, Catalog.coverage))).all()
    by_title = dict(rows)
    assert by_title["Lirtuel"] == CoverageScope.SUBDIVISIONS
    assert by_title["Project Gutenberg"] is None
    assert sum(value is None for value in by_title.values()) == SEED_CATALOG_COUNT - 1


async def test_the_seed_input_is_rejected_by_the_published_schema(db_session: AsyncSession) -> None:
    """ADR-030, stated as a test: this is *why* the relaxed schema exists. If this ever
    passes, the published schema has loosened and the relaxed variant may be unnecessary."""
    feed = json.loads(SEED_FILE.read_text(encoding="utf-8"))

    errors = list(build_schema_validator("feed.schema.json").iter_errors(feed))
    assert errors, "data/recommended.json now satisfies feed.schema.json — revisit ADR-030"


async def test_re_running_after_the_file_grows_adds_only_the_new_rows(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Hadrien said he will probably extend the file."""
    await seed_catalogs(db_session, SEED_FILE)
    await db_session.commit()

    feed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    feed["catalogs"].append(
        {
            "metadata": {"title": "Added Later", "kind": ["public"]},
            "links": [{"href": "https://later.example/opds", "rel": "catalog"}],
        }
    )
    extended = tmp_path / "recommended.json"
    extended.write_text(json.dumps(feed), encoding="utf-8")

    created, updated = await seed_catalogs(db_session, extended)
    await db_session.commit()

    assert (created, updated) == (1, SEED_CATALOG_COUNT)
    assert await count(db_session, Catalog) == SEED_CATALOG_COUNT + 1


def test_identity_is_the_catalog_rel_href_not_the_self_href() -> None:
    """Q1 — every `self` href points at edrlab.github.io and changes at cutover."""
    document = {
        "metadata": {"title": "Example"},
        "links": [
            {"href": "https://edrlab.github.io/x.json", "rel": "self"},
            {"href": "https://library.example/home.opds2", "rel": "catalog"},
        ],
    }

    assert resolve_identity_href(document) == "https://library.example/home.opds2"


def test_shelf_is_the_documented_fallback_when_there_is_no_catalog_link() -> None:
    document = {
        "metadata": {"title": "Example"},
        "links": [{"href": "https://library.example/shelf.opds2", "rel": "shelf"}],
    }

    assert resolve_identity_href(document) == "https://library.example/shelf.opds2"


def test_a_catalog_with_no_identity_link_is_rejected() -> None:
    document = {
        "metadata": {"title": "Example"},
        "links": [{"href": "https://edrlab.github.io/x.json", "rel": "self"}],
    }

    with pytest.raises(ValidationError, match="no stable identity"):
        resolve_identity_href(document)
