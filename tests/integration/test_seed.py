"""The seed path, against real Postgres."""

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import import_catalog_document, resolve_identity_href, seed_catalogs
from registry.core.errors import ValidationError
from registry.core.schema_validation import build_schema_validator
from registry.db.models.catalog import Catalog, CatalogLanguageRow
from registry.db.models.link import Link
from registry.domain.enums import CatalogStatus, CoverageScope, LinkRel
from tests.conftest import SEED_CATALOG_COUNT, SEED_FILE

pytestmark = pytest.mark.integration


async def count(session: AsyncSession, model: type) -> int:
    return (await session.scalar(select(func.count()).select_from(model))) or 0


async def test_seed_imports_every_recommended_catalog(db_session: AsyncSession) -> None:
    """Presence in the file is the recommended flag."""
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
    """Tags land lowercase or not at all.

    The input is written uppercase here rather than read from the seed file. The file itself
    is lowercase now, so seeding it would assert nothing: this test has to supply the casing
    it is defending against. BCP-47 declares tags case-insensitive and real producers do send
    `EN`, so the ingest path must fold them whatever the repository's own fixtures look like.
    """
    document = {
        "metadata": {
            "title": "Uppercase Tags",
            "identifier": "urn:uuid:2569a0f1-5a98-4d33-ae92-fb26671ba2e0",
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

    Lowercasing on ingest is the rule that must hold for any input. This asserts the
    repository's own fixtures are already canonical, so a diff never turns on casing.
    """
    for catalog in json.loads(SEED_FILE.read_text(encoding="utf-8"))["catalogs"]:
        tags = catalog["metadata"].get("supportedLanguages", [])
        assert tags == [tag.lower() for tag in tags], catalog["metadata"]["title"]


async def test_undeclared_coverage_is_stored_as_null(db_session: AsyncSession) -> None:
    """A catalog that did not declare coverage gets NULL, never `global`.

    None of the seed catalogs declares it, so the second half of the rule, that a declared
    value survives ingest, is asserted here on a document written for the purpose.
    """
    await seed_catalogs(db_session, SEED_FILE)
    document = {
        "metadata": {
            "title": "Declares Coverage",
            "identifier": "urn:uuid:a7ed9044-af69-434f-b82b-bbbe541e2074",
            "kind": ["public"],
            "coverage": "country",
            "country": "BE",
        },
        "links": [{"href": "https://example.org/opds", "rel": "catalog"}],
    }
    await import_catalog_document(db_session, document, recommended=True)
    await db_session.commit()

    rows = (await db_session.execute(select(Catalog.title, Catalog.coverage))).all()
    by_title = dict(rows)
    assert by_title["Declares Coverage"] == CoverageScope.COUNTRY
    assert by_title["Project Gutenberg"] is None
    assert sum(value is None for value in by_title.values()) == SEED_CATALOG_COUNT


async def test_the_seed_input_is_rejected_by_the_published_schema(db_session: AsyncSession) -> None:
    """Stated as a test: this is *why* the relaxed schema exists. If this ever
    passes, the published schema has loosened and the relaxed variant may be unnecessary."""
    feed = json.loads(SEED_FILE.read_text(encoding="utf-8"))

    errors = list(build_schema_validator("feed.schema.json").iter_errors(feed))
    assert errors, "data/recommended.json now satisfies feed.schema.json. Revisit"


async def test_re_running_after_the_file_grows_adds_only_the_new_rows(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Hadrien said he will probably extend the file."""
    await seed_catalogs(db_session, SEED_FILE)
    await db_session.commit()

    feed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    feed["catalogs"].append(
        {
            "metadata": {
                "title": "Added Later",
                "identifier": "urn:uuid:fe4ed407-6968-458b-9f87-da9b59306c3f",
                "kind": ["public"],
            },
            "links": [{"href": "https://later.example/opds", "rel": "catalog"}],
        }
    )
    extended = tmp_path / "recommended.json"
    extended.write_text(json.dumps(feed), encoding="utf-8")

    created, updated = await seed_catalogs(db_session, extended)
    await db_session.commit()

    assert (created, updated) == (1, SEED_CATALOG_COUNT)
    assert await count(db_session, Catalog) == SEED_CATALOG_COUNT + 1


def test_identity_is_the_browsable_href_not_the_document_identifier() -> None:
    """The `catalog` link, not `self` and not `metadata.identifier`.

    `metadata.identifier` is present and ignored: the registry renders that field from
    `catalogs.id`, so the value in the source document never reaches the database.
    """
    document = {
        "metadata": {
            "title": "Example",
            "identifier": "urn:uuid:38f6d219-c567-44c5-8dc5-1b8a85ed9b69",
        },
        "links": [
            {"href": "https://edrlab.github.io/x.json", "rel": "self"},
            {"href": "https://library.example/home.opds2", "rel": "catalog"},
        ],
    }

    assert resolve_identity_href(document) == "https://library.example/home.opds2"


def test_a_catalog_without_a_browsable_link_is_rejected() -> None:
    document = {
        "metadata": {
            "title": "Example",
            "identifier": "urn:uuid:38f6d219-c567-44c5-8dc5-1b8a85ed9b69",
        },
        "links": [{"href": "https://edrlab.github.io/x.json", "rel": "self"}],
    }

    with pytest.raises(ValidationError, match="no stable identity"):
        resolve_identity_href(document)


async def test_the_document_identifier_is_not_the_one_that_is_stored(
    db_session: AsyncSession,
) -> None:
    """The seed reads past `metadata.identifier`: `catalogs.id` is generated by Postgres.

    A hand-assigned value in `data/recommended.json` is therefore free to be anything, and is
    never what a client sees rendered back.
    """
    document = {
        "metadata": {
            "title": "Hand-assigned Identifier",
            "identifier": "urn:uuid:30a59158-28bc-4fc1-ad99-93325968d9c1",
            "kind": ["open"],
        },
        "links": [{"href": "https://example.org/opds", "rel": "catalog"}],
    }
    catalog, _ = await import_catalog_document(db_session, document, recommended=True)
    await db_session.commit()

    assert str(catalog.id) != "30a59158-28bc-4fc1-ad99-93325968d9c1"


async def test_two_catalogs_cannot_share_an_identity_href(db_session: AsyncSession) -> None:
    """uq_links_identity_href. The identity is enforced in the database, not only looked up.

    `import_catalog_document` reads before it writes, and a check-then-insert is not atomic:
    two concurrent seeds would both find nothing and commit the same catalog twice. The race
    itself is impractical to stage in a test, so what is asserted here is the constraint that
    makes the losing insert fail. Inserting the rows directly bypasses the read that would
    otherwise turn the second one into an update.
    """
    for title in ("First Claimant", "Second Claimant"):
        db_session.add(
            Catalog(
                title=title,
                status=CatalogStatus.ACTIVE,
                published_at=func.now(),
                recommended=True,
                links=[Link(href="https://contested.example/opds", rel=LinkRel.CATALOG)],
            )
        )

    with pytest.raises(IntegrityError, match="uq_links_identity_href"):
        await db_session.commit()


async def test_a_changed_href_inserts_a_second_catalog(db_session: AsyncSession) -> None:
    """The accepted cost of href matching, asserted rather than described.

    The scenario is Project Gutenberg moving pre-prod to prod. Identity is the browsable href,
    so a catalog that changes its feed URL is a new row, not an update — the operator's fix is
    to wipe and re-seed. If this ever stops being acceptable, identity has to move back onto
    something the source document owns.
    """
    original = {
        "metadata": {"title": "Moving Library", "kind": ["open"]},
        "links": [{"href": "https://old.example/opds", "rel": "catalog"}],
    }
    await import_catalog_document(db_session, original, recommended=True)
    await db_session.commit()

    moved = {
        "metadata": {"title": "Moving Library", "kind": ["open"]},
        "links": [{"href": "https://new.example/opds", "rel": "catalog"}],
    }
    _, created = await import_catalog_document(db_session, moved, recommended=True)
    await db_session.commit()

    assert created is True
    assert await count(db_session, Catalog) == 2


async def test_a_catalog_removed_from_the_file_stays_recommended(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """The known gap, asserted rather than described.

    Presence in the file is meant to be the recommended flag, but `add` writes rows the file
    never mentions, and nothing on the row says where it came from. Reconciling would
    unrecommend those. If this test ever fails, provenance has been added and
    `seed_catalogs` should reconcile.
    """
    await seed_catalogs(db_session, SEED_FILE)
    await db_session.commit()

    feed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    dropped = feed["catalogs"].pop()["metadata"]["title"]
    shortened = tmp_path / "recommended.json"
    shortened.write_text(json.dumps(feed), encoding="utf-8")

    await seed_catalogs(db_session, shortened)
    await db_session.commit()

    rows = dict((await db_session.execute(select(Catalog.title, Catalog.recommended))).all())
    assert rows[dropped] is True, "unrecommending needs provenance; see seed_catalogs"


async def test_seeding_leaves_a_separately_added_catalog_alone(
    db_session: AsyncSession,
) -> None:
    """The reason the gap above is not closed by reconciling.

    `add` writes a recommended row the seed file has never heard of. A later `make seed` must
    not touch it, which is what an unconditional reconciliation would do.
    """
    await import_catalog_document(
        db_session,
        {
            "metadata": {
                "title": "Added Separately",
                "identifier": "urn:uuid:c24b85b8-8a33-4382-8b0d-bb687e708cc6",
                "kind": ["public"],
            },
            "links": [{"href": "https://example.org/opds", "rel": "catalog"}],
        },
        recommended=True,
    )
    await db_session.commit()

    await seed_catalogs(db_session, SEED_FILE)
    await db_session.commit()

    recommended = (await db_session.scalars(select(Catalog.title).where(Catalog.recommended))).all()
    assert "Added Separately" in recommended
    assert len(recommended) == SEED_CATALOG_COUNT + 1


async def test_a_reactivated_catalog_gets_a_publication_date(db_session: AsyncSession) -> None:
    """ck_catalogs_published_when_active. A suggested row has none, and activating it without
    one fails the constraint at commit rather than in the code."""
    document = {
        "metadata": {
            "title": "Was Suggested",
            "identifier": "urn:uuid:3169c992-a585-46cd-9825-3c3453ceaa08",
            "kind": ["public"],
        },
        "links": [{"href": "https://example.org/opds", "rel": "catalog"}],
    }
    catalog, _ = await import_catalog_document(db_session, document, recommended=False)
    catalog.status = CatalogStatus.SUGGESTED
    catalog.published_at = None
    await db_session.commit()

    await import_catalog_document(db_session, document, recommended=True)
    await db_session.commit()

    row = (
        await db_session.execute(select(Catalog).where(Catalog.title == "Was Suggested"))
    ).scalar_one()
    assert row.status == CatalogStatus.ACTIVE
    assert row.published_at is not None


async def test_a_rel_array_is_accepted(db_session: AsyncSession) -> None:
    """Readium's link schema allows `rel` to be an array, so the seed path must read one."""
    document = {
        "metadata": {
            "title": "Array Rel",
            "identifier": "urn:uuid:511be66e-1406-4d55-b29c-2c6547e242ba",
            "kind": ["open"],
        },
        "links": [{"href": "https://example.org/opds", "rel": ["catalog", "start"]}],
    }

    catalog, _ = await import_catalog_document(db_session, document, recommended=True)
    await db_session.commit()

    assert {link.rel for link in catalog.links} == {LinkRel.CATALOG}
