"""Import a feed-shaped document into the registry. Idempotent.

`data/recommended.json` is the default source, and presence in *that* file is the recommended
flag. `data/libraries.json` is the second data set and is imported `--no-recommended`: its
catalogs are `active`, so they are real published catalogs reachable at `/catalogs/{id}`, but
they stay out of the top-level feed. `demo/` is example output and the contract-test corpus.

The input is validated against the *relaxed* schema derived by
`scripts/generate_seed_schema.py`, not against `catalog.schema.json`. The file has no
feed-level `links` and no `self` link on any catalog, so the published schema rejects every
record. `self` is synthesised at render time from the registry's own base URL, and the
contract tests validate the output.

**A catalog is matched across re-seeds by its id when the document names one, and by its
`catalog`/`shelf` link href otherwise**; see `resolve_existing_catalog`. `catalogs.id` is the
registry's only UUID; `metadata.identifier` is rendered *from* that id rather than stored beside
it. The href is externally owned rather than stable, so a document relying on it carries that
cost; one carrying an identifier does not.

The id itself is **derived, not invented**; see `resolve_catalog_id`. A document that
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

from sqlalchemy import select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from registry.core.config import Settings
from registry.core.errors import ValidationError
from registry.core.schema_validation import SEED_INPUT_SCHEMA, build_schema_validator
from registry.db.models.catalog import (
    Catalog,
)
from registry.db.models.reference import Country, Subdivision
from registry.db.session import build_session_factory, create_database_engine
from registry.domain.catalog_document import (
    build_catalog_id,
    build_consistency_problems,
    build_identity_href,
    build_iso_code_problems,
)
from registry.domain.enums import CatalogStatus
from registry.repositories.catalog_documents import build_catalog, persist_catalog_contents
from registry.repositories.catalog_repository import CatalogRepository

#: Presence in `data/recommended.json` is the recommended flag.
RECOMMENDED_AT_LAUNCH = True


# Limit: the href match is only the fallback now, but a catalog with no `metadata.identifier`
# whose file changes its feed URL inserts a second row instead of updating the first. The fix is
# to give it an identifier, which `resolve_existing_catalog` then matches on.
def resolve_identity_href(document: dict[str, Any]) -> str:
    """The externally owned URL this catalog is matched on across seed runs.

    Used when the document supplies no `metadata.identifier`, and always as the value the id
    is derived from. `resolve_existing_catalog` prefers the id when there is one, because the
    href belongs to the library and can change under us.
    """
    if (href := build_identity_href(document)) is not None:
        return href
    raise ValidationError(
        f"{document['metadata']['title']} has neither a `catalog` nor a `shelf` link, "
        "so it has no stable identity to upsert on"
    )


def resolve_catalog_id(document: dict[str, Any], identity_href: str) -> uuid.UUID:
    """The UUID this catalog is stored under, derived rather than generated.

    A supplied `metadata.identifier` wins: it is the author's own name for the catalog, so the
    file can name the id a client will fetch it at. Note the limit: this decides the id a row is
    *inserted* with. Adding an identifier to a catalog already stored does not move it onto that
    id, because an upsert does not rewrite a primary key
    (`test_an_identifier_added_later_does_not_move_an_existing_row`). When the field is absent
    the id is `uuid5(NAMESPACE_URL, identity_href)`, a name-based UUID (RFC 9562 §5.5), so the
    same catalog lands on the same UUID in local Docker, in CI and on Cloud Run, and keeps it
    across a wipe and re-seed.

    The alternative, letting `gen_random_uuid()` decide, produces an id that exists only in
    whichever database happened to run the seed. A catalog that is not `recommended` appears
    in no feed, so that id would be unreachable except by querying Postgres directly.
    """
    identifier = document["metadata"].get("identifier")
    if identifier:
        # RFC 9562 §4: the `urn:uuid:` prefix is case-insensitive, so `URN:UUID:` is the same
        # URN. The published schema's pattern only admits the lowercase form, but this function
        # is also reachable from code that has not been through it.
        trimmed = identifier[9:] if identifier[:9].lower() == "urn:uuid:" else identifier
        try:
            return uuid.UUID(trimmed)
        except ValueError as error:
            raise ValidationError(
                f"{document['metadata']['title']} has an identifier that is not a UUID: "
                f"{identifier!r}"
            ) from error
    # Limit: derived from the href, so it inherits the href's instability: a catalog that
    # moves its feed URL gets a new id as well as a new row. Supplying `metadata.identifier`
    # is the escape hatch, and is what to reach for if that becomes a problem.
    return build_catalog_id(identity_href)


async def resolve_existing_catalog(
    session: AsyncSession, document: dict[str, Any], identity_href: str, catalog_id: uuid.UUID
) -> Catalog | None:
    """The row this document updates, or None to insert. Two lookups, id first.

    **A document that names its own id is matched on that id before its href.** Without this,
    a catalog whose feed URL changed would miss the href lookup, take the insert path, and
    collide on `pk_catalogs`, because the id is derived from the unchanged
    `metadata.identifier`. That is a hard failure that aborts the whole seed transaction, so
    the other catalogs in the file are not updated either.

    It is also the point of the field. The identifiers exist so that a library moving from
    pre-production to production stays one row (the Project Gutenberg case); matching on them
    is what delivers that.

    The href lookup remains, and remains the fallback, for the two cases where it is the only
    identity available: `data/libraries.json`, which supplies no identifiers, and a catalog
    already stored under a different id than the one the file now names, which keeps the id it
    has, since an upsert does not rewrite a primary key.

    **A document without an identifier that misses on href is then looked up by its derived
    id.** An editor can change a catalog's main link, and the row keeps the id derived from the
    old one. The file still names the old href, so the href lookup misses, and inserting would
    collide on `pk_catalogs` and abort the whole seed. Matching on the id updates that row
    instead, and its link goes back to the file's.
    """
    repository = CatalogRepository(session)
    named = bool(document["metadata"].get("identifier"))
    if named and (by_id := await repository.fetch_catalog_by_identity_id(catalog_id)) is not None:
        return by_id
    by_href = await repository.fetch_catalog_by_identity_href(identity_href)
    if by_href is not None or named:
        return by_href
    return await repository.fetch_catalog_by_identity_id(catalog_id)


def assert_iso_codes_are_uppercase(document: dict[str, Any]) -> None:
    """Refuse a `country` or `subdivisions` entry that is not a well-formed uppercase code.

    Raises `ValidationError`. ISO 3166-1 and 3166-2 codes are uppercase, and the registry enforces
    that rather than folding it: a lowercase code in a data file is an authoring mistake its author
    should see, not something the importer silently rewrites.

    The generated input schema already rejects most of these, so for `seed` and `add` this rarely
    fires. It exists because the schema is only one of the ways into the write path.
    `build_catalog` and `import_catalog_document` are public, called directly by tests today and by
    whatever drives the back office later, and without this a bad code would reach Postgres and
    fail on `ck_catalogs_country_uppercase` or `ck_catalog_subdivisions_subdivision_code_uppercase`
    as an `IntegrityError` naming a constraint instead of the value.

    It checks the whole shape, not just the case. `value != value.upper()` would be the obvious
    test and it lets `"BE\n"` through, because a trailing newline has no case. Anything that is not
    a string, an empty string and free text such as `"Belgium"` are refused as malformed rather than
    "corrected", since there is no sensible uppercase form to suggest.

    `supportedLanguages` is deliberately not checked here. BCP-47 declares case insignificant and
    writes a region subtag uppercase, so `normalise_language_tag` folds those on ingest instead.
    """
    metadata = document["metadata"]
    problems = [problem.message for problem in build_iso_code_problems(metadata)]

    if problems:
        raise ValidationError(
            f"{metadata['title']} has invalid ISO codes: {'; '.join(problems)}. These are "
            "uppercase by specification and are enforced rather than converted, so fix them at "
            "the source."
        )


def assert_document_is_consistent(document: dict[str, Any]) -> None:
    """Refuse what the schema cannot see and the database would refuse by constraint name: a
    repeated kind, language, publication type or subdivision, a templated link that is not
    `search`, a repeated link, or `catalog` and `shelf` links sharing an href. Raises
    `ValidationError`. The same checks the admin API reports field by field
    (`build_consistency_problems`)."""
    problems = [problem.message for problem in build_consistency_problems(document)]
    if problems:
        raise ValidationError(f"{document['metadata']['title']} is invalid: {'; '.join(problems)}.")


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

    # Limit: created_at doubles as the sort key, so a row's position is spelled as a time
    # it was not created at. An explicit ordering column is the upgrade if the feed ever
    # needs an order the seed file cannot express.
    if ordered_at is None:
        ordered_at = datetime.now(UTC)
    assert_iso_codes_are_uppercase(document)
    identity_href = resolve_identity_href(document)
    assert_document_is_consistent(document)
    catalog_id = resolve_catalog_id(document, identity_href)
    existing = await resolve_existing_catalog(session, document, identity_href, catalog_id)
    built = build_catalog(document, catalog_id, recommended=recommended)

    if existing is None:
        built.published_at = func.now()
        built.created_at = ordered_at
        session.add(built)
        return built, True

    await persist_catalog_contents(session, existing, built)
    existing.recommended = recommended
    # The admin API's optimistic-concurrency check compares this, so an editor's open page
    # must see a re-import as a change rather than overwrite it.
    existing.version = Catalog.version + 1
    # ck_catalogs_published_when_published. A row that was a draft has no publication date,
    # and publishing it without one fails the constraint at commit.
    if existing.published_at is None:
        existing.published_at = func.now()
    # A re-import publishes a draft, as it always activated a suggestion. It does not undo an
    # editor: an unpublished or archived catalog stays that way.
    if existing.status is CatalogStatus.DRAFT:
        existing.status = built.status
    existing.created_at = ordered_at
    return existing, False


def assert_identities_are_unique(feed: dict[str, Any], *, source: str) -> None:
    """Refuse a document where two catalogs resolve to the same id. Raises `ValidationError`.

    Two entries sharing a `metadata.identifier` used to be two rows, because ids were random.
    Now the second one's id lookup finds the row the first one just inserted (pending inserts
    are visible to the next query through autoflush) and *overwrites it in place*: one row,
    the first catalog's title and links gone, `created 1, updated 1` printed, no error. A typo
    in a hand-edited file silently deletes a catalog.

    JSON Schema cannot express "unique within this document", so it is checked here, before
    anything is written.

    **Both keys the upsert can match on are checked, not just the id.** `resolve_existing_catalog`
    falls back to the href, so two entries with *different* explicit identifiers and the same
    `catalog`/`shelf` href collide by the other route: the second one misses the id lookup, finds
    the first by href, and overwrites it. Checking ids alone left that open. Href is checked
    first, because a document with no identifier derives its id *from* the href and would
    otherwise be reported as an identifier clash it does not have.

    This pre-empts `uq_links_identity_href`, which would have caught the href case in the
    database, but only once the write was attempted and without naming either catalog.
    """
    by_href: dict[str, str] = {}
    by_id: dict[uuid.UUID, str] = {}

    for document in feed["catalogs"]:
        title = document["metadata"]["title"]
        identity_href = resolve_identity_href(document)
        catalog_id = resolve_catalog_id(document, identity_href)

        if (owner := by_href.get(identity_href)) is not None:
            raise ValidationError(
                f"{source} gives {owner!r} and {title!r} the same `catalog`/`shelf` href "
                f"({identity_href}), so the second would replace the first. Two catalogs cannot "
                "share the href they are matched on."
            )
        if (owner := by_id.get(catalog_id)) is not None:
            raise ValidationError(
                f"{source} gives {owner!r} and {title!r} the same id ({catalog_id}), so the "
                "second would replace the first. Two catalogs cannot share an id, whether it is "
                "written as `metadata.identifier` or derived from the `catalog`/`shelf` href."
            )

        by_href[identity_href] = title
        by_id[catalog_id] = title


async def assert_places_are_known(
    session: AsyncSession, feed: dict[str, Any], *, source: str
) -> None:
    """Refuse unknown country and subdivision codes, and subdivisions outside their catalog's
    country, with a message. Raises `ValidationError`.

    `subdivisions` is **not** a full copy of ISO 3166-2. It holds only the codes some catalog
    references, added per data migration. So a valid code the table has never heard of fails on
    `fk_catalog_subdivisions_subdivision_code_subdivisions` at flush time, as a raw
    `IntegrityError` that names a constraint rather than the code, the catalog or the remedy.
    `make add --subdivision NL-ZH` is the easiest way to hit it; the flag's own help says
    "ISO 3166-2", which is a promise five rows cannot keep. A well-formed country the
    `countries` table lacks (`XK`) fails the same way on `fk_catalogs_country_code_countries`.

    A subdivision must also belong to its catalog's country, the rule the admin API applies:
    `FR` with `BE-WAL` contradicts itself, and subdivisions without a country have none to
    belong to.

    One query for the whole document, before anything is written, listing every unknown code at
    once, so an operator fixes one migration rather than rediscovering this per catalog.
    """
    metadata = [document["metadata"] for document in feed["catalogs"]]
    countries = {item["country"] for item in metadata if item.get("country")}
    subdivisions = {code for item in metadata for code in item.get("subdivisions") or ()}
    if not countries and not subdivisions:
        return

    # Country and subdivision codes never coincide (`BE` against `BE-WAL`), so one map holds
    # both: each known code to the country it is in.
    statement = union_all(
        select(Country.alpha2, Country.alpha2).where(Country.alpha2.in_(countries)),
        select(Subdivision.code, Subdivision.country_alpha2).where(
            Subdivision.code.in_(subdivisions)
        ),
    )
    known = dict((await session.execute(statement)).tuples().all())

    if unknown := [
        f"{item['title']} ({item['country']})"
        for item in metadata
        if item.get("country") and item["country"] not in known
    ]:
        raise ValidationError(
            f"{source} names a country absent from the `countries` table, which holds the "
            f"ISO 3166-1 codes: {', '.join(unknown)}."
        )
    if unknown := sorted(subdivisions - known.keys()):
        raise ValidationError(
            f"{source} references {', '.join(unknown)}, absent from the `subdivisions` table. "
            "That table holds only the ISO 3166-2 codes some catalog already uses, so a valid "
            "code can still be missing; add it in a migration, as `b41f7c9ade52` did for "
            "FR-IDF and CH-VS."
        )
    for item in metadata:
        country = item.get("country")
        if outside := [c for c in item.get("subdivisions") or () if known[c] != country]:
            raise ValidationError(
                f"{item['title']} names {', '.join(outside)}, outside its country "
                f"({country or 'none given'}). A subdivision must belong to the catalog's "
                "`country`."
            )


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

    assert_identities_are_unique(feed, source=source)
    await assert_places_are_known(session, feed, source=source)

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
    try:
        feed = json.loads(seed_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        # Otherwise a hand-edit with a trailing comma is a traceback rather than a message.
        raise ValidationError(f"{seed_file} is not valid JSON. {error}") from error

    return await import_feed_document(session, feed, source=str(seed_file), recommended=recommended)


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

    `--no-recommended` is the one that matters: getting it wrong seeds `data/libraries.json`
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
    parser = build_seed_parser()
    arguments = parser.parse_args(argv)
    # `--no-recommended` against the default file would flip every catalog in
    # `data/recommended.json` out of the top-level feed in one command, which contradicts
    # `seed_catalogs`' promise that unrecommending is a deliberate manual step. The flag
    # describes a *file*, so it has to name one.
    if not arguments.recommended and arguments.file is None:
        parser.error("--no-recommended needs a file; it must not be used on REGISTRY_SEED_FILE")

    settings = Settings()
    seed_file = arguments.file or settings.seed_file
    # Checked here rather than left to `read_text`: `FileNotFoundError` is an `OSError`, and
    # `cli/__main__.py` reports every `OSError` as an unreachable database, so a typo in the
    # path prints "Is it running? make up" and never mentions the file.
    if not seed_file.is_file():
        raise ValidationError(f"{seed_file} is not a file. Nothing to seed.")

    created, updated = asyncio.run(
        seed_from_file(seed_file, settings, recommended=arguments.recommended)
    )
    flag = "" if arguments.recommended else ", not recommended"
    print(f"seeded {seed_file}: {created} created, {updated} updated{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
