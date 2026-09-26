"""Import a feed-shaped document into the registry. Idempotent.

`data/recommended.json` is the default source, and presence in *that* file is the recommended
flag. `data/libraries.json` is the second data set and is imported `--no-recommended`: its
catalogs are `active`, so they are real published catalogs reachable at `/catalogs/{id}`, but
they stay out of the top-level feed. `demo/` is example output and the contract-test corpus.

the input is validated against the *relaxed* schema derived by
`scripts/generate_seed_schema.py`, not against `catalog.schema.json`. The file has no
feed-level `links` and no `self` link on any catalog, so the published schema rejects every
record. `self` is synthesised at render time from the registry's own base URL, and the
contract tests validate the output.

**A catalog is matched across re-seeds by its id when the document names one, and by its
`catalog`/`shelf` link href otherwise** — see `resolve_existing_catalog`. `catalogs.id` is the
registry's only UUID; `metadata.identifier` is rendered *from* that id rather than stored beside
it. The href is externally owned rather than stable, so a document relying on it carries that
cost; one carrying an identifier does not.

The id itself is **derived, not invented** — see `resolve_catalog_id`. A document that
supplies `metadata.identifier` keeps that UUID; one that omits it gets a UUID computed from
its identity href, which is the same value in every environment and across a wipe. Neither
path lets Postgres pick, because a randomly picked id cannot be addressed by anyone who has
only the source file.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
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
from registry.domain.links import IDENTITY_RELS
from registry.repositories.catalog_repository import CatalogRepository

#: Presence in `data/recommended.json` is the recommended flag.
RECOMMENDED_AT_LAUNCH = True

#: Derived from the published schemas by `scripts/generate_seed_schema.py`.
SEED_INPUT_SCHEMA = "generated/seed-input.schema.json"


def link_rels(link: dict[str, Any]) -> list[LinkRel]:
    """The rels on *link* that this registry stores, in the order they were written.

    A Readium link's ``rel`` is a string *or* an array (`link.schema.json`), and it may name
    relations the registry has no column for. `start` is a real OPDS rel and not one of ours;
    it is skipped rather than raising, exactly as the `add` path skips it. A link left with no
    known rel contributes nothing, which is what `next` and `self` already do.
    """
    rel = link.get("rel")
    written = rel if isinstance(rel, list) else [rel]
    return [LinkRel(value) for value in written if value in set(LinkRel)]


# ponytail: href match, and only the fallback now - a catalog with no `metadata.identifier`
# that changes its feed URL inserts a second row instead of updating the first. The fix is to
# give it an identifier, which `resolve_existing_catalog` then matches on.
def resolve_identity_href(document: dict[str, Any]) -> str:
    """The externally owned URL this catalog is matched on across seed runs.

    Used when the document supplies no `metadata.identifier`, and always as the value the id
    is derived from. `resolve_existing_catalog` prefers the id when there is one, because the
    href belongs to the library and can change under us.
    """
    for rel in IDENTITY_RELS:
        for link in document["links"]:
            if rel in link_rels(link):
                return str(link["href"])
    raise ValidationError(
        f"{document['metadata']['title']} has neither a `catalog` nor a `shelf` link, "
        "so it has no stable identity to upsert on"
    )


def resolve_catalog_id(document: dict[str, Any], identity_href: str) -> uuid.UUID:
    """The UUID this catalog is stored under, derived rather than generated.

    A supplied `metadata.identifier` wins: it is the author's own name for the catalog, and
    honouring it means editing the file can address an existing row. When the field is absent
    the id is `uuid5(NAMESPACE_URL, identity_href)` — a name-based UUID (RFC 9562 §5.5), so the
    same catalog lands on the same UUID in local Docker, in CI and on Cloud Run, and keeps it
    across a wipe and re-seed.

    The alternative, letting `gen_random_uuid()` decide, produces an id that exists only in
    whichever database happened to run the seed. A catalog that is not `recommended` appears
    in no feed, so that id would be unreachable except by querying Postgres directly.
    """
    identifier = document["metadata"].get("identifier")
    if identifier:
        return uuid.UUID(identifier.removeprefix("urn:uuid:"))
    # ponytail: derived from the href, so it inherits the href's instability - a catalog that
    # moves its feed URL gets a new id as well as a new row. Supplying `metadata.identifier`
    # is the escape hatch, and is what to reach for if that becomes a problem.
    return uuid.uuid5(uuid.NAMESPACE_URL, identity_href)


async def resolve_existing_catalog(
    session: AsyncSession, document: dict[str, Any], identity_href: str, catalog_id: uuid.UUID
) -> Catalog | None:
    """The row this document updates, or None to insert. Two lookups, id first.

    **A document that names its own id is matched on that id before its href.** Without this,
    a catalog whose feed URL changed would miss the href lookup, take the insert path, and
    collide on `pk_catalogs` — because the id is derived from the unchanged
    `metadata.identifier`. That is a hard failure that aborts the whole seed transaction, so
    the other catalogs in the file are not updated either.

    It is also the point of the field. Hadrien assigned those identifiers so that "a library
    moving pre-prod to prod" stays one row (the Project Gutenberg case); matching on them is
    what finally delivers that. See ADR-038.

    The href lookup remains, and remains the fallback, for the two cases where it is the only
    identity available: `data/libraries.json`, which supplies no identifiers, and a catalog
    already stored under a different id than the one the file now names — which keeps the id it
    has, since an upsert does not rewrite a primary key.
    """
    repository = CatalogRepository(session)
    if document["metadata"].get("identifier"):
        by_id = await repository.fetch_catalog_by_identity_id(catalog_id)
        if by_id is not None:
            return by_id
    return await repository.fetch_catalog_by_identity_href(identity_href)


def build_catalog(document: dict[str, Any], catalog_id: uuid.UUID, *, recommended: bool) -> Catalog:
    """*catalog_id* comes from `resolve_catalog_id`. The caller resolves it, because it is also
    what `resolve_existing_catalog` looks the row up by."""
    metadata = document["metadata"]

    return Catalog(
        id=catalog_id,
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
        # One row per rel: a link declaring `["catalog", "start"]` is two relations to the
        # same href, and the wire contract carries a single rel per link.
        links=[
            Link(
                href=link["href"],
                media_type=link.get("type"),
                rel=rel,
                templated=bool(link.get("templated", False)),
                title=link.get("title"),
            )
            for link in document["links"]
            for rel in link_rels(link)
        ],
    )


async def import_catalog_document(
    session: AsyncSession,
    document: dict[str, Any],
    *,
    recommended: bool,
    ordered_at: datetime | None = None,
) -> tuple[Catalog, bool]:
    """Insert, or replace the existing catalog's contents in place. Returns (catalog, created).

    *ordered_at* is the `created_at` to write, and `created_at` is what the feed orders on:
    the newest row leads its language bucket. `import_feed_document` derives it from the
    catalog's position in the file, so the file's order is the feed's order. Left out, it is
    simply now.

    Taking the timestamp rather than the position is deliberate. The caller reads the clock
    once for the whole run; reading it per catalog let the time each row spends in the
    database outrun the gap between positions, which reversed the order.

    Re-seeding rewrites it, so editing the file is how the order is changed.
    """
    # Imported here, not at module level: only the write path needs it.
    from sqlalchemy import func  # noqa: PLC0415

    # ponytail: created_at doubles as the sort key, so a row's position is spelled as a time
    # it was not created at. An explicit ordering column is the upgrade if the feed ever
    # needs an order the seed file cannot express.
    if ordered_at is None:
        ordered_at = datetime.now(UTC)
    identity_href = resolve_identity_href(document)
    catalog_id = resolve_catalog_id(document, identity_href)
    existing = await resolve_existing_catalog(session, document, identity_href, catalog_id)
    built = build_catalog(document, catalog_id, recommended=recommended)

    if existing is None:
        built.published_at = func.now()
        built.created_at = ordered_at
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
    # ck_catalogs_published_when_active. A row that was `suggested` has no publication date,
    # and activating it without one fails the constraint at commit.
    if existing.published_at is None:
        existing.published_at = func.now()
    existing.status = built.status
    existing.created_at = ordered_at
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

    # One clock reading for the run: each catalog is written a millisecond earlier than the
    # one before it, so position in the file survives as `created_at` order. Re-read per
    # catalog, the write latency between rows would swamp that gap and invert it.
    anchor = datetime.now(UTC)

    created = updated = 0
    for position, document in enumerate(feed["catalogs"]):
        _, was_created = await import_catalog_document(
            session,
            document,
            recommended=recommended,
            ordered_at=anchor - timedelta(milliseconds=position),
        )
        created += was_created
        updated += not was_created

    return created, updated


async def seed_catalogs(
    session: AsyncSession, seed_file: Path, *, recommended: bool = RECOMMENDED_AT_LAUNCH
) -> tuple[int, int]:
    """Upsert every catalog in *seed_file*. See `import_feed_document`.

    **Removing a catalog from the file does not unrecommend it.** The module says presence in
    the file *is* the recommended flag, and that was true when the file was the only way in.
    `add` broke the premise: the database now holds rows the file has never mentioned, and
    nothing distinguishes "was seeded, then removed from the file" from "was never in the
    file". Reconciling against everything recommended unrecommends catalogs added by `add`,
    which is worse than the gap it closes.

    Closing it properly needs provenance on the row, a column recording where a catalog came
    from, which is a schema change and a decision for the project lead rather than something
    to infer here. Until then, unrecommending is a manual step. There is a test asserting the
    current behaviour so the gap is executable rather than a comment.
    """
    return await import_feed_document(
        session,
        json.loads(seed_file.read_text(encoding="utf-8")),
        source=str(seed_file),
        recommended=recommended,
    )


async def seed_from_file(
    seed_file: Path, settings: Settings, *, recommended: bool = RECOMMENDED_AT_LAUNCH
) -> tuple[int, int]:
    engine = create_database_engine(settings)
    try:
        async with build_session_factory(engine)() as session:
            created, updated = await seed_catalogs(session, seed_file, recommended=recommended)
            await session.commit()
    finally:
        await engine.dispose()
    return created, updated


def build_seed_parser() -> argparse.ArgumentParser:
    """Separate from `main` so the flags are testable without a database.

    `--no-recommended` is the one that matters: getting it wrong seeds Hadrien's library data
    straight into the top-level feed, which is silent and wrong rather than loud and wrong.
    """
    parser = argparse.ArgumentParser(
        prog="python -m registry.cli seed",
        description="Import a feed-shaped document into the registry. Idempotent.",
    )
    parser.add_argument(
        "file",
        nargs="?",
        type=Path,
        help="feed-shaped JSON to import. Defaults to REGISTRY_SEED_FILE",
    )
    parser.add_argument(
        "--no-recommended",
        dest="recommended",
        action="store_false",
        help="import the catalogs active but not recommended, so they stay out of the feed "
        "and are reachable only at /catalogs/{id}",
    )
    return parser


def main(argv: Sequence[str] = ()) -> int:
    """`REGISTRY_SEED_FILE` remains the default so `make seed` needs no argument.

    A named file is the second data set's route in: `data/libraries.json` is imported
    `--no-recommended`, which is the only difference between it and `data/recommended.json`
    as far as this command is concerned. Both go through `seed_catalogs`.
    """
    arguments = build_seed_parser().parse_args(argv)

    settings = Settings()
    seed_file = arguments.file or settings.seed_file
    created, updated = asyncio.run(
        seed_from_file(seed_file, settings, recommended=arguments.recommended)
    )
    flag = "" if arguments.recommended else ", not recommended"
    print(f"seeded {seed_file}: {created} created, {updated} updated{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
