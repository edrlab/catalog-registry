"""The seed path, against real Postgres."""

import json
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import (
    import_catalog_document,
    import_feed_document,
    seed_catalogs,
)
from registry.core.errors import ValidationError
from registry.core.schema_validation import build_schema_validator
from registry.db.models.catalog import Catalog, CatalogLanguageRow
from registry.db.models.link import Link
from registry.domain.enums import CatalogStatus, CoverageScope, LinkRel
from registry.repositories.catalog_repository import CatalogRepository
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


async def test_the_document_identifier_becomes_the_stored_id(
    db_session: AsyncSession,
) -> None:
    """A supplied `metadata.identifier` *is* `catalogs.id`, stripped of its `urn:uuid:` prefix.

    This is what makes the file authoritative: the author can name a catalog and then fetch
    `/catalogs/{that uuid}` without first asking the database what id it invented.
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

    assert str(catalog.id) == "30a59158-28bc-4fc1-ad99-93325968d9c1"


async def test_a_derived_id_survives_a_re_seed(db_session: AsyncSession) -> None:
    """The point of deriving it. An id anyone writes down has to still be there afterwards."""
    document = {
        "metadata": {"title": "Derived", "kind": ["public"]},
        "links": [{"href": "https://library.example/home.opds2", "rel": "catalog"}],
    }
    first, created = await import_catalog_document(db_session, document, recommended=False)
    await db_session.commit()
    first_id = first.id

    second, recreated = await import_catalog_document(db_session, document, recommended=False)
    await db_session.commit()

    assert (created, recreated) == (True, False)
    assert second.id == first_id
    assert await count(db_session, Catalog) == 1


async def test_a_not_recommended_import_stays_out_of_the_feed_query(
    db_session: AsyncSession,
) -> None:
    """`data/libraries.json`'s whole contract: active, real, and not in the top-level feed.

    Hadrien's words were "they can be active as long as they're not recommended". `status` and
    `recommended` are separate columns, so this asserts both halves at once.
    """
    document = {
        "metadata": {"title": "Unrecommended Library", "kind": ["public"]},
        "links": [{"href": "https://library.example/home.opds2", "rel": "catalog"}],
    }
    catalog, _ = await import_catalog_document(db_session, document, recommended=False)
    await db_session.commit()

    assert catalog.recommended is False
    assert catalog.status is CatalogStatus.ACTIVE
    assert await CatalogRepository(db_session).fetch_recommended_catalogs() == []
    assert await CatalogRepository(db_session).fetch_catalog_by_id(catalog.id) is not None


async def test_uppercase_country_and_subdivisions_are_stored_verbatim(
    db_session: AsyncSession,
) -> None:
    """No case folding. The codes reach the database exactly as the file wrote them.

    Hadrien, 2026-09-29, on which case to standardise: "I think that officially it's all uppercase
    for ISO 3166-2", and "it's better to enforce this at import than convert". So uppercase is the
    only accepted form, the input schema rejects anything else, and nothing here calls `.upper()`.
    `test_lowercase_codes_are_rejected_rather_than_converted` is the other half.
    """
    document = {
        "metadata": {
            "title": "Uppercase Codes",
            "kind": ["public"],
            "country": "BE",
            "subdivisions": ["BE-WAL", "BE-BRU"],
        },
        "links": [{"href": "https://library.example/home.opds2", "rel": "catalog"}],
    }
    catalog, _ = await import_catalog_document(db_session, document, recommended=False)
    await db_session.commit()

    assert catalog.country_code == "BE"
    assert sorted(row.subdivision_code for row in catalog.subdivisions) == ["BE-BRU", "BE-WAL"]


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        pytest.param({"country": "be"}, "country 'be' should be 'BE'", id="country-lower"),
        pytest.param({"country": "Be"}, "country 'Be' should be 'BE'", id="country-mixed"),
        pytest.param(
            {"country": "BE", "subdivisions": ["be-wal"]},
            "subdivisions 'be-wal' should be 'BE-WAL'",
            id="subdivision-lower",
        ),
        pytest.param(
            {"country": "BE", "subdivisions": ["Be-Wal"]},
            "subdivisions 'Be-Wal' should be 'BE-WAL'",
            id="subdivision-mixed",
        ),
        pytest.param(
            {"country": "BE", "subdivisions": ["BE-BRU", "be-wal"]},
            "subdivisions 'be-wal' should be 'BE-WAL'",
            id="one-bad-among-good",
        ),
        # Not lowercase at all, so there is no uppercase form to suggest. A trailing newline is the
        # important one: it has no case, `value != value.upper()` passes it, and the schema's
        # `^[A-Z]{2}$` is applied with `re.search`, where `$` also matches before a final newline.
        pytest.param(
            {"country": "BE\n"}, "country 'BE\\n' is not a well-formed", id="country-newline"
        ),
        pytest.param(
            {"country": "BE", "subdivisions": ["FR-IDF\n"]},
            "subdivisions 'FR-IDF\\n' is not a well-formed",
            id="subdivision-newline",
        ),
        pytest.param({"country": ""}, "country '' is not a well-formed", id="country-empty"),
        pytest.param(
            {"country": "Belgium"}, "country 'Belgium' is not a well-formed", id="free-text"
        ),
        pytest.param({"country": " BE"}, "country ' BE' is not a well-formed", id="leading-space"),
        pytest.param({"country": "B\u0130"}, "is not a well-formed", id="non-ascii-capital"),
        pytest.param({"country": 7}, "country 7 is not a well-formed", id="not-a-string"),
        pytest.param(
            {"country": "BE", "subdivisions": [None]},
            "subdivisions None is not a well-formed",
            id="null-subdivision",
        ),
    ],
)
async def test_the_core_refuses_malformed_codes_even_without_schema_validation(
    db_session: AsyncSession, metadata: dict[str, object], expected: str
) -> None:
    """Layer two. `import_catalog_document` does not validate against the schema, so it needs its
    own check, or a caller that skips validation reaches Postgres and fails on a check constraint
    naming the constraint instead of the value.

    Most cases here are also caught by the schema on the `seed` and `add` paths. The newline ones
    are not: they get through the schema, so this is the layer that actually stops them.
    """
    document = {
        "metadata": {"title": "Lowercase Codes", "kind": ["public"], **metadata},
        "links": [{"href": "https://library.example/home.opds2", "rel": "catalog"}],
    }

    with pytest.raises(ValidationError, match="invalid ISO codes") as failure:
        await import_catalog_document(db_session, document, recommended=False)

    assert expected in str(failure.value)
    assert await count(db_session, Catalog) == 0


async def test_lowercase_codes_are_rejected_by_input_validation(db_session: AsyncSession) -> None:
    """Layer one, and the one an author actually hits: the schema, naming both fields at once."""
    feed = {
        "metadata": {"title": "Lowercase"},
        "catalogs": [
            {
                "metadata": {
                    "title": "Lowercase Codes",
                    "kind": ["public"],
                    "country": "be",
                    "subdivisions": ["be-wal"],
                },
                "links": [{"href": "https://library.example/home.opds2", "rel": "catalog"}],
            }
        ],
    }

    with pytest.raises(ValidationError, match="not valid seed input") as failure:
        await import_feed_document(db_session, feed, source="test", recommended=False)

    message = str(failure.value)
    assert "'be' does not match" in message
    assert "'be-wal' does not match" in message
    assert await count(db_session, Catalog) == 0


async def test_the_country_check_constraint_is_what_refuses_a_lowercase_country(
    db_session: AsyncSession,
) -> None:
    """Layer three, for `catalogs.country_code`. Raw SQL, since that is the only way past layers one
    and two, and a constraint nothing exercises is one somebody drops by accident.

    The row is otherwise valid: `zz` is added to `countries` first, so the foreign key is satisfied
    and the check constraint is the only thing that can object. Asserting on its *name* is what
    makes this fail if the constraint is removed, instead of passing on some unrelated error.
    """
    await db_session.execute(
        text("INSERT INTO countries (alpha2, alpha3, numeric3) VALUES ('zz', 'zzz', '999')")
    )

    with pytest.raises(IntegrityError, match="ck_catalogs_country_uppercase"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO catalogs"
                    " (id, status, recommended, title, country_code, published_at)"
                    " VALUES (gen_random_uuid(), 'active', false, 'Lower', 'zz', now())"
                )
            )


async def test_the_subdivision_check_constraint_is_what_refuses_a_lowercase_code(
    db_session: AsyncSession,
) -> None:
    """Layer three, for `catalog_subdivisions.subdivision_code`. Same construction as above.

    A real catalog and a `subdivisions` row with a lowercase code both exist, so both foreign keys
    are satisfied and only `ck_catalog_subdivisions_subdivision_code_uppercase` can object.
    `subdivisions.code` has no case check of its own, which is what makes the row insertable.
    """
    document = {
        "metadata": {"title": "Parent", "kind": ["public"], "country": "BE"},
        "links": [{"href": "https://library.example/home.opds2", "rel": "catalog"}],
    }
    catalog, _ = await import_catalog_document(db_session, document, recommended=False)
    await db_session.flush()
    await db_session.execute(
        text("INSERT INTO subdivisions (code, country_alpha2) VALUES ('be-xyz', 'BE')")
    )

    with pytest.raises(IntegrityError, match="ck_catalog_subdivisions_subdivision_code_uppercase"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO catalog_subdivisions (catalog_id, subdivision_code)"
                    " VALUES (:id, 'be-xyz')"
                ),
                {"id": catalog.id},
            )


async def test_an_uppercase_language_tag_is_still_lowercased(db_session: AsyncSession) -> None:
    """Languages keep the opposite treatment, and that is not an oversight.

    BCP-47 says case is insignificant and writes regions uppercase, `Accept-Language` arrives
    lowercase, and the feed compares tags as strings. So language tags are folded on ingest while
    ISO codes are refused. A regression that "made case handling consistent" would break the
    language bucket silently, which is why this sits next to the tests above.
    """
    document = {
        "metadata": {
            "title": "Uppercase Language",
            "kind": ["public"],
            "country": "BE",
            "supportedLanguages": ["FR", "en-GB"],
        },
        "links": [{"href": "https://library.example/home.opds2", "rel": "catalog"}],
    }
    catalog, _ = await import_catalog_document(db_session, document, recommended=False)
    await db_session.commit()

    assert sorted(row.language_tag for row in catalog.languages) == ["en-gb", "fr"]
    assert catalog.country_code == "BE"


@pytest.mark.parametrize("second_rel", [LinkRel.CATALOG, LinkRel.SHELF])
async def test_two_catalogs_cannot_share_an_identity_href(
    db_session: AsyncSession, second_rel: LinkRel
) -> None:
    """uq_links_identity_href. The identity is enforced in the database, not only looked up.

    `import_catalog_document` reads before it writes, and a check-then-insert is not atomic:
    two concurrent seeds would both find nothing and commit the same catalog twice. The race
    itself is impractical to stage in a test, so what is asserted here is the constraint that
    makes the losing insert fail. Inserting the rows directly bypasses the read that would
    otherwise turn the second one into an update.

    Both rels, because the lookup spans both: a URL that is one catalog's `catalog` link and
    another's `shelf` link would match two rows, and the import would update whichever came
    back first. One URL is one catalog either way.
    """
    for title, rel in (("First Claimant", LinkRel.CATALOG), ("Second Claimant", second_rel)):
        db_session.add(
            Catalog(
                title=title,
                status=CatalogStatus.ACTIVE,
                published_at=func.now(),
                recommended=True,
                links=[Link(href="https://contested.example/opds", rel=rel)],
            )
        )

    with pytest.raises(IntegrityError, match="uq_links_identity_href"):
        await db_session.commit()


async def test_a_changed_href_inserts_a_second_catalog(db_session: AsyncSession) -> None:
    """The accepted cost of href matching, for a document with **no** identifier to match on.

    This is `data/libraries.json`'s position, not `data/recommended.json`'s: with no
    `metadata.identifier`, identity is the browsable href, so changing the feed URL is a new row
    rather than an update. The fix per catalog is to give it an identifier, which
    `test_an_identified_catalog_survives_an_href_change` asserts; failing that, wipe and re-seed.
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


async def test_an_identified_catalog_survives_an_href_change(db_session: AsyncSession) -> None:
    """The Project Gutenberg pre-prod to prod move, which `metadata.identifier` exists for.

    Regression test for a real defect: deriving `catalogs.id` from the identifier while matching
    only on the href meant a moved catalog missed the lookup, took the insert path, and violated
    `pk_catalogs`, aborting the whole seed transaction, so every other catalog in the file went
    un-updated too. `resolve_existing_catalog` matches on the id first.
    """
    identifier = "urn:uuid:7dadebbe-5276-42f4-aa7f-4c8631c965e3"
    original = {
        "metadata": {"title": "Moving Library", "identifier": identifier, "kind": ["open"]},
        "links": [{"href": "https://old.example/opds", "rel": "catalog"}],
    }
    first, _ = await import_catalog_document(db_session, original, recommended=True)
    await db_session.commit()

    moved = {
        "metadata": {"title": "Moving Library", "identifier": identifier, "kind": ["open"]},
        "links": [{"href": "https://new.example/opds", "rel": "catalog"}],
    }
    second, created = await import_catalog_document(db_session, moved, recommended=True)
    await db_session.commit()

    assert created is False
    assert second.id == first.id
    assert await count(db_session, Catalog) == 1
    assert [link.href for link in second.links] == ["https://new.example/opds"]


async def test_an_identifier_added_later_does_not_move_an_existing_row(
    db_session: AsyncSession,
) -> None:
    """The fallback the href lookup still exists for.

    A catalog first seeded without an identifier holds a derived id. Giving it an explicit
    identifier afterwards must not insert a second row: the id lookup misses, the href lookup
    finds it, and it keeps the id it has, because an upsert does not rewrite a primary key. The
    documented way to make the two agree is a wipe and re-seed.
    """
    href = "https://library.example/home.opds2"
    before = {
        "metadata": {"title": "Late Identifier", "kind": ["open"]},
        "links": [{"href": href, "rel": "catalog"}],
    }
    first, _ = await import_catalog_document(db_session, before, recommended=True)
    await db_session.commit()

    after = {
        "metadata": {
            "title": "Late Identifier",
            "identifier": "urn:uuid:c0ffee00-0000-4000-8000-000000000001",
            "kind": ["open"],
        },
        "links": [{"href": href, "rel": "catalog"}],
    }
    second, created = await import_catalog_document(db_session, after, recommended=True)
    await db_session.commit()

    assert created is False
    assert second.id == first.id == uuid.uuid5(uuid.NAMESPACE_URL, href)
    assert await count(db_session, Catalog) == 1


async def test_two_catalogs_sharing_an_identifier_are_refused(db_session: AsyncSession) -> None:
    """Without this the second one silently overwrites the first, and the counts look normal.

    Ids used to be random, so two entries were two rows. Once the id is derived from
    `metadata.identifier`, the second entry's id lookup finds the row the first just inserted
    (autoflush makes a pending insert visible) and replaces its title and links in place. One
    row, `1 created, 1 updated`, no error, one catalog gone. JSON Schema cannot express
    "unique within this document", so `assert_identities_are_unique` checks it before any write.
    """
    shared = "urn:uuid:11111111-2222-4333-8444-555555555555"
    feed = {
        "metadata": {"title": "Duplicated"},
        "catalogs": [
            {
                "metadata": {"title": "First", "identifier": shared, "kind": ["open"]},
                "links": [{"href": "https://a.example/opds", "rel": "catalog"}],
            },
            {
                "metadata": {"title": "Second", "identifier": shared, "kind": ["open"]},
                "links": [{"href": "https://b.example/opds", "rel": "catalog"}],
            },
        ],
    }

    blames_identifier = re.escape("cannot share an id")
    with pytest.raises(ValidationError, match=blames_identifier) as failure:
        await import_feed_document(db_session, feed, source="test")

    assert "'First' and 'Second' the same id" in str(failure.value)
    assert await count(db_session, Catalog) == 0


async def test_two_catalogs_sharing_an_href_blame_the_href_not_the_identifier(
    db_session: AsyncSession,
) -> None:
    """The likelier collision, and it must not send the operator to the wrong field.

    `data/libraries.json` carries no identifiers, so two of its entries sharing a `catalog` href
    derive the same `uuid5` and trip the same guard. Blaming `metadata.identifier` there names a
    field neither document has.
    """
    feed = {
        "metadata": {"title": "Duplicated"},
        "catalogs": [
            {
                "metadata": {"title": "Branch A", "kind": ["public"]},
                "links": [{"href": "https://library.example/shared.opds2", "rel": "catalog"}],
            },
            {
                "metadata": {"title": "Branch B", "kind": ["public"]},
                "links": [{"href": "https://library.example/shared.opds2", "rel": "catalog"}],
            },
        ],
    }

    blames_href = re.escape("the same `catalog`/`shelf` href")
    with pytest.raises(ValidationError, match=blames_href) as failure:
        await import_feed_document(db_session, feed, source="test", recommended=False)

    assert "`metadata.identifier`" not in str(failure.value)
    assert await count(db_session, Catalog) == 0


async def test_malformed_json_is_a_readable_message(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text('{"metadata": {"title": "x"},}', encoding="utf-8")

    with pytest.raises(ValidationError, match="is not valid JSON"):
        await seed_catalogs(db_session, broken)


async def test_a_libraries_shaped_document_imports_through_the_validating_path(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Everything `data/libraries.json` relies on, in one test through `seed_catalogs`.

    A self-contained fixture rather than the real file, so it keeps asserting the same thing if that
    file changes: no `metadata.identifier`, the `FR-IDF` and `CH-VS` rows added by `b41f7c9ade52`,
    and `recommended=False`. `test_the_real_libraries_file_imports` covers the file itself.
    """
    href = "https://bibliotheques.paris.fr/numerique/"
    seed_file = tmp_path / "libraries.json"
    seed_file.write_text(
        json.dumps(
            {
                "metadata": {"title": "Libraries"},
                "catalogs": [
                    {
                        "metadata": {
                            "title": "Bibliothèque numérique de Paris",
                            "kind": ["public"],
                            "country": "FR",
                            "subdivisions": ["FR-IDF"],
                            "coverage": "subdivisions",
                        },
                        "links": [{"href": href, "type": "text/html", "rel": "catalog"}],
                    },
                    {
                        "metadata": {
                            "title": "Médiathèque Valais",
                            "kind": ["public"],
                            "country": "CH",
                            "subdivisions": ["CH-VS"],
                        },
                        "links": [
                            {"href": "https://library.example/valais.opds2", "rel": "catalog"}
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    created, updated = await seed_catalogs(db_session, seed_file, recommended=False)
    await db_session.commit()

    assert (created, updated) == (2, 0)
    paris = await CatalogRepository(db_session).fetch_catalog_by_identity_href(href)
    assert paris is not None
    assert paris.id == uuid.uuid5(uuid.NAMESPACE_URL, href)
    assert paris.country_code == "FR"
    assert [row.subdivision_code for row in paris.subdivisions] == ["FR-IDF"]
    assert paris.recommended is False
    assert paris.status is CatalogStatus.ACTIVE
    assert await CatalogRepository(db_session).fetch_recommended_catalogs() == []


async def test_the_real_libraries_file_imports(db_session: AsyncSession) -> None:
    """`data/libraries.json` itself, end to end, as `make seed-libraries` runs it.

    The self-contained test above pins what the file *relies on*; this one pins that the file in
    the repository still imports, so an edit to it that the schema accepts but the database
    rejects (an unseeded subdivision, say) fails here rather than on somebody's machine. Each
    catalog must also be reachable at the id its `catalog` href derives, which is the whole point:
    these are in no feed, so that computed id is the only way to find them.
    """
    libraries = SEED_FILE.parent / "libraries.json"
    document = json.loads(libraries.read_text(encoding="utf-8"))

    created, updated = await seed_catalogs(db_session, libraries, recommended=False)
    await db_session.commit()

    assert (created, updated) == (len(document["catalogs"]), 0)
    repository = CatalogRepository(db_session)
    assert await repository.fetch_recommended_catalogs() == []
    for entry in document["catalogs"]:
        href = next(link["href"] for link in entry["links"] if link["rel"] == "catalog")
        stored = await repository.fetch_catalog_by_id(uuid.uuid5(uuid.NAMESPACE_URL, href))
        assert stored is not None, entry["metadata"]["title"]
        assert stored.recommended is False
        assert stored.status is CatalogStatus.ACTIVE

    again = await seed_catalogs(db_session, libraries, recommended=False)
    assert again == (0, len(document["catalogs"]))


async def test_an_unseeded_subdivision_is_a_readable_message(db_session: AsyncSession) -> None:
    """`subdivisions` is not a full copy of ISO 3166-2, so a valid code can still be missing.

    It used to fail at flush as a raw `IntegrityError` naming
    `fk_catalog_subdivisions_subdivision_code_subdivisions`: not the code, not the catalog, not
    the remedy. `make add --subdivision NL-ZH` is the easy way in, and that flag's help says
    "ISO 3166-2", which five rows cannot deliver. Every unknown code is listed at once, so one
    migration fixes the document.
    """
    feed = {
        "metadata": {"title": "Dutch"},
        "catalogs": [
            {
                "metadata": {
                    "title": "Dutch Library",
                    "kind": ["public"],
                    "country": "NL",
                    "subdivisions": ["NL-ZH", "NL-UT"],
                },
                "links": [{"href": "https://library.example/nl.opds2", "rel": "catalog"}],
            }
        ],
    }

    with pytest.raises(ValidationError, match="NL-UT, NL-ZH"):
        await import_feed_document(db_session, feed, source="test")

    assert await count(db_session, Catalog) == 0


async def test_a_seeded_subdivision_passes_the_check(db_session: AsyncSession) -> None:
    """The codes `b41f7c9ade52` added are present, so the guard is not simply rejecting."""
    feed = {
        "metadata": {"title": "Swiss"},
        "catalogs": [
            {
                "metadata": {
                    "title": "Valais",
                    "kind": ["public"],
                    "country": "CH",
                    "subdivisions": ["CH-VS"],
                },
                "links": [{"href": "https://library.example/valais.opds2", "rel": "catalog"}],
            }
        ],
    }

    created, _ = await import_feed_document(db_session, feed, source="test", recommended=False)
    await db_session.commit()

    assert created == 1


async def test_different_identifiers_with_one_href_are_refused(
    db_session: AsyncSession,
) -> None:
    """The hole in an id-only uniqueness check, found by Copilot on the PR.

    Both keys the upsert can match on have to be checked. With distinct identifiers the two ids
    differ, so an id-only guard passes them; then the second document misses `by_id`, falls back
    to the href, finds the row the first one just inserted, and replaces its title and links.
    One row, `1 created, 1 updated`, no error, one catalog silently gone.
    """
    href = "https://library.example/shared.opds2"
    feed = {
        "metadata": {"title": "Duplicated"},
        "catalogs": [
            {
                "metadata": {
                    "title": "First",
                    "identifier": "urn:uuid:11111111-2222-4333-8444-555555555555",
                    "kind": ["open"],
                },
                "links": [{"href": href, "rel": "catalog"}],
            },
            {
                "metadata": {
                    "title": "Second",
                    "identifier": "urn:uuid:66666666-7777-4888-8999-aaaaaaaaaaaa",
                    "kind": ["open"],
                },
                "links": [{"href": href, "rel": "catalog"}],
            },
        ],
    }

    blames_href = re.escape("the same `catalog`/`shelf` href")
    with pytest.raises(ValidationError, match=blames_href) as failure:
        await import_feed_document(db_session, feed, source="test")

    assert "'First' and 'Second'" in str(failure.value)
    assert await count(db_session, Catalog) == 0


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
