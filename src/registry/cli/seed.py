"""Import `data/recommended.json` into the registry. Idempotent.

`data/recommended.json` is the seed source, and presence in the file *is* the
recommended flag. `demo/` is example output and the contract-test corpus; `archive/` is out
of scope for v0.

the input is validated against the *relaxed* schema derived by
`scripts/generate_seed_schema.py`, not against `catalog.schema.json`. The file has no
feed-level `links` and no `self` link on any catalog, so the published schema rejects every
record. `self` is synthesised at render time from the registry's own base URL, and the
contract tests validate the output.

**The upsert conflict target is still an open question.** `id` is generated, so it never
conflicts. The current answer is the **`catalog` rel href**, the library's own
OPDS feed URL. It is externally owned and stable, where every `self` href in the fixtures
points at `edrlab.github.io/catalog-registry/...` and so changes at cutover, which would
silently duplicate every catalog exactly once.

A catalog with a `shelf` link but no `catalog` link is permitted by the validation rule, so
`shelf` is the documented fallback. If neither is present the record is rejected rather than
inserted under an identity that cannot be matched again.

"""

import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from registry.core.config import Settings
from registry.core.errors import ValidationError
from registry.core.schema_validation import build_schema_validator
from registry.db.models.catalog import (
    Catalog,
    CatalogKindRow,
    CatalogLanguageRow,
    CatalogPublicationTypeRow,
    CatalogSubdivisionRow,
)
from registry.db.models.link import Link
from registry.db.session import build_session_factory, create_database_engine
from registry.domain.enums import (
    CatalogColor,
    CatalogKind,
    CatalogStatus,
    CoverageScope,
    LinkRel,
    PublicationType,
)
from registry.domain.language import normalise_language_tag
from registry.domain.links import has_browsable_rel
from registry.repositories.catalog_repository import CatalogRepository

#: Presence in `data/recommended.json` is the recommended flag.
RECOMMENDED_AT_LAUNCH = True

#: Derived from the published schemas by `scripts/generate_seed_schema.py`.
SEED_INPUT_SCHEMA = "generated/seed-input.schema.json"

#: Preferred identity first, fallback second.
IDENTITY_RELS = (LinkRel.CATALOG, LinkRel.SHELF)


def resolve_identity_href(document: dict[str, Any]) -> str:
    """The externally owned URL this catalog is matched on across seed runs."""
    for rel in IDENTITY_RELS:
        for link in document["links"]:
            if link.get("rel") == rel.value:
                return str(link["href"])
    raise ValidationError(
        f"{document['metadata']['title']} has neither a `catalog` nor a `shelf` link, "
        "so it has no stable identity to upsert on"
    )


def build_catalog(document: dict[str, Any], *, recommended: bool) -> Catalog:
    metadata = document["metadata"]
    # `self` is not required on input; it is synthesised at render time. What the
    # registry cannot synthesise is somewhere to actually browse or borrow.
    rels = [LinkRel(link["rel"]) for link in document["links"]]
    if not has_browsable_rel(rels):
        raise ValidationError(f"{metadata['title']} needs a `catalog` or `shelf` link")

    return Catalog(
        status=CatalogStatus.ACTIVE,
        recommended=recommended,
        title=metadata["title"],
        description=metadata.get("description"),
        color=CatalogColor(metadata.get("color", CatalogColor.GRAY.value)),
        country_code=(metadata["country"].upper() if metadata.get("country") else None),
        city=metadata.get("city"),
        # Absent means NULL, not `global`. Storing an undeclared coverage as
        # `global` would assert worldwide reach on the catalog's behalf.
        coverage=(CoverageScope(metadata["coverage"]) if metadata.get("coverage") else None),
        # ck_catalogs_published_when_active: an active catalog must carry a publication date.
        published_at=None,
        kinds=[CatalogKindRow(kind=CatalogKind(value)) for value in metadata["kind"]],
        publication_types=[
            CatalogPublicationTypeRow(publication_type=PublicationType(value))
            for value in metadata.get("publicationTypes", ())
        ],
        # Lowercase on ingest. The repository's own fixtures are already lowercase, so
        # this is not defensive: BCP-47 declares tags case-insensitive and real producers do
        # send `EN`. The check constraint rejects anything else, so a regression fails loudly.
        languages=[
            CatalogLanguageRow(language_tag=normalise_language_tag(value))
            for value in metadata.get("supportedLanguages", ())
        ],
        subdivisions=[
            CatalogSubdivisionRow(subdivision_code=value.upper())
            for value in metadata.get("subdivisions", ())
        ],
        links=[
            Link(
                href=link["href"],
                media_type=link.get("type"),
                rel=LinkRel(link["rel"]),
                templated=bool(link.get("templated", False)),
                title=link.get("title"),
            )
            for link in document["links"]
        ],
    )


async def import_catalog_document(
    session: AsyncSession, document: dict[str, Any], *, recommended: bool
) -> tuple[Catalog, bool]:
    """Insert, or replace the existing catalog's contents in place. Returns (catalog, created)."""
    # Imported here, not at module level: only the write path needs it.
    from sqlalchemy import func  # noqa: PLC0415

    identity = resolve_identity_href(document)
    existing = await CatalogRepository(session).fetch_catalog_by_self_href(identity)
    built = build_catalog(document, recommended=recommended)

    if existing is None:
        built.published_at = func.now()
        session.add(built)
        return built, True

    # Replace the child collections wholesale rather than diffing them: the source document
    # is the whole truth for this catalog, and a diff is more code for the same result.
    for model, column in (
        (CatalogKindRow, CatalogKindRow.catalog_id),
        (CatalogPublicationTypeRow, CatalogPublicationTypeRow.catalog_id),
        (CatalogLanguageRow, CatalogLanguageRow.catalog_id),
        (CatalogSubdivisionRow, CatalogSubdivisionRow.catalog_id),
        (Link, Link.catalog_id),
    ):
        await session.execute(delete(model).where(column == existing.id))

    existing.title = built.title
    existing.description = built.description
    existing.color = built.color
    existing.country_code = built.country_code
    existing.city = built.city
    existing.coverage = built.coverage
    existing.recommended = recommended
    existing.status = built.status
    existing.kinds = built.kinds
    existing.publication_types = built.publication_types
    existing.languages = built.languages
    existing.subdivisions = built.subdivisions
    existing.links = built.links
    return existing, False


async def import_feed_document(
    session: AsyncSession,
    feed: dict[str, Any],
    *,
    source: str,
    recommended: bool = RECOMMENDED_AT_LAUNCH,
) -> tuple[int, int]:
    """Validate a feed-shaped document, then upsert every catalog in it.

    Returns (created, updated). *source* names the document in the error message, a file
    path for `seed`, a URL for `add`. Takes a session rather than making one, so tests can
    run it inside their transaction and the caller owns the commit.
    """
    errors = sorted(build_schema_validator(SEED_INPUT_SCHEMA).iter_errors(feed), key=str)
    if errors:
        detail = "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors)
        raise ValidationError(f"{source} is not valid seed input. {detail}")

    created = updated = 0
    for document in feed["catalogs"]:
        _, was_created = await import_catalog_document(session, document, recommended=recommended)
        created += was_created
        updated += not was_created

    return created, updated


async def seed_catalogs(
    session: AsyncSession, seed_file: Path, *, recommended: bool = RECOMMENDED_AT_LAUNCH
) -> tuple[int, int]:
    """Upsert every catalog in *seed_file*. See `import_feed_document`."""
    return await import_feed_document(
        session,
        json.loads(seed_file.read_text(encoding="utf-8")),
        source=str(seed_file),
        recommended=recommended,
    )


async def seed_from_file(seed_file: Path, settings: Settings) -> tuple[int, int]:
    engine = create_database_engine(settings)
    try:
        async with build_session_factory(engine)() as session:
            created, updated = await seed_catalogs(session, seed_file)
            await session.commit()
    finally:
        await engine.dispose()
    return created, updated


def main(argv: Sequence[str] = ()) -> int:
    if argv:
        print(f"seed takes no arguments, got {' '.join(argv)}")
        return 2
    settings = Settings()
    created, updated = asyncio.run(seed_from_file(settings.seed_file, settings))
    print(f"seeded: {created} created, {updated} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
