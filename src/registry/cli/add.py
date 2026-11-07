"""Import one catalog into the registry from its live OPDS feed URL.

    python -m registry.cli add https://example.org/opds --kind public

The feed supplies what a machine can know: its title, and the links it publishes. Everything
else. Kind, colour, coverage, country, languages, publication types. Is editorial and comes
from flags, because no OPDS feed declares any of it. `--kind` is required for that reason:
`catalog.schema.json` gives it `minItems: 1`, and there is nothing to derive it from.

This is not a replacement for `seed`. `data/recommended.json` remains the bootstrap
the thing that turns an empty database into a working one. This writes the same
rows, through the same validation and the same upsert, driven from a terminal instead of a
file. The back office (v1.0) will drive that same path from a browser.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from registry.cli.seed import assert_iso_codes_are_uppercase, import_feed_document
from registry.core.config import Settings
from registry.core.errors import ValidationError
from registry.db.session import build_session_factory, create_database_engine
from registry.domain.enums import CatalogColor, CatalogKind, CoverageScope, LinkRel, PublicationType
from registry.domain.feed_check import (
    OPDS_MEDIA_TYPE,
    build_feed_links,
    normalise_remote_rel,
    parse_json_document,
)
from registry.domain.fetching import FetchError, FetchResult
from registry.network.safe_client import SafeHttpClient

__all__ = ["build_catalog_document", "download_feed_document", "main", "normalise_remote_rel"]

#: A registry entry is metadata, not a mirror. Twelve seconds and two megabytes is a generous
#: ceiling for a feed's first page, and it bounds a hostile or broken origin.
FETCH_TIMEOUT_SECONDS = 12.0
MAX_FEED_BYTES = 2 * 1024 * 1024
FEED_ACCEPT = "application/opds+json, application/json;q=0.9"


def build_outbound_client() -> SafeHttpClient:
    """The client `add` fetches with: the registry's one outbound client. A function so
    the loopback tests can swap the transport, never the checks."""
    return SafeHttpClient()


async def _download(url: str) -> FetchResult:
    async with build_outbound_client() as client:
        return await client.fetch(
            url, accept=FEED_ACCEPT, max_bytes=MAX_FEED_BYTES, deadline=FETCH_TIMEOUT_SECONDS
        )


def download_feed_document(url: str) -> dict[str, Any]:
    """Fetch and parse an OPDS feed. Raises `ValidationError` on anything unusable.

    `SafeHttpClient` refuses anything but a public http(s) address, on the URL given and on every
    redirect, and connects only to the address it checked.
    """
    try:
        result = asyncio.run(_download(url))
    except FetchError as error:
        raise ValidationError(f"could not fetch {url}: {error.detail}") from None
    if not 200 <= result.status < 300:  # noqa: PLR2004
        raise ValidationError(f"could not fetch {url}: the server answered {result.status}")
    try:
        feed = parse_json_document(result.body)
    except ValueError as error:
        raise ValidationError(f"{url} did not return JSON. {error}") from None
    if not isinstance(feed, dict):
        raise ValidationError(f"{url} returned {type(feed).__name__}, not an OPDS document")
    return feed


def build_catalog_document(feed: dict[str, Any], url: str, **editorial: Any) -> dict[str, Any]:
    """The seed-shaped catalog document for *feed*, fetched from *url*.

    *url* becomes the `catalog` rel, and that is also the upsert identity: a re-run of `add`
    on the same URL finds the row it created the first time and replaces its contents rather
    than inserting a second one. The feed's own links are kept by the feed check's rules
    (`build_feed_links`): known rels only, hrefs made absolute, http(s) only.

    `editorial` holds the flag-supplied metadata; keys whose value is `None` or empty are
    dropped, so `metadata` never carries a null that `additionalProperties: false` would
    otherwise have to allow.
    """
    metadata_in = feed.get("metadata")
    title = metadata_in.get("title") if isinstance(metadata_in, dict) else None
    if not isinstance(title, str) or not title.strip():
        raise ValidationError(f"{url} has no metadata.title, so there is nothing to name it")

    links: list[dict[str, Any]] = [
        {"href": url, "type": OPDS_MEDIA_TYPE, "rel": LinkRel.CATALOG.value}
    ]
    for link in build_feed_links(feed.get("links"), url):
        imported: dict[str, Any] = {"href": link.href, "rel": link.rel}
        if link.type:
            imported["type"] = link.type
        if link.templated:
            imported["templated"] = True
        links.append(imported)

    # No `identifier` is generated. The seed input schema no longer requires one, and the
    # import derives `catalogs.id` from this document's `catalog` href, which is *url*, so
    # a re-`add` of the same feed lands on the same id instead of a fresh random one.
    metadata: dict[str, Any] = {"title": title.strip()}
    metadata.update({key: value for key, value in editorial.items() if value})
    return {"metadata": metadata, "links": links}


async def add_catalog_from_url(url: str, document: dict[str, Any], settings: Settings) -> bool:
    """Upsert one catalog document. Returns True if it was created, False if it replaced one.

    Deduplication is the shared import path's: it matches on the `catalog` rel href, which is
    the *url* this document was built from, so a re-`add` of the same URL updates that row.
    """
    feed = {"metadata": {"title": f"Imported from {url}"}, "catalogs": [document]}
    engine = create_database_engine(settings)
    try:
        async with build_session_factory(engine)() as session:
            created, _ = await import_feed_document(session, feed, source=url)
            await session.commit()
    finally:
        await engine.dispose()
    return bool(created)


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m registry.cli add",
        description="Import a catalog into the registry from its live OPDS feed URL.",
    )
    parser.add_argument("url", help="the catalog's OPDS feed. Becomes its `catalog` link")
    parser.add_argument(
        "--kind",
        action="append",
        required=True,
        choices=[kind.value for kind in CatalogKind],
        help="repeatable; required, because no feed declares it",
    )
    parser.add_argument("--color", choices=[color.value for color in CatalogColor])
    parser.add_argument("--description")
    parser.add_argument("--city")
    parser.add_argument("--country", help="ISO 3166-1 alpha-2, uppercase: BE")
    parser.add_argument("--coverage", choices=[scope.value for scope in CoverageScope])
    parser.add_argument("--language", action="append", help="BCP-47; repeatable")
    parser.add_argument(
        "--subdivision",
        action="append",
        help="ISO 3166-2, uppercase, repeatable: BE-WAL. Must be in the `subdivisions` table "
        "and belong to --country; anything else is refused",
    )
    parser.add_argument(
        "--publication-type",
        action="append",
        choices=[kind.value for kind in PublicationType],
        help="repeatable",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the document that would be imported and write nothing",
    )
    arguments = parser.parse_args(argv)
    # Before the download: the flags are the only source of these codes, so a typo should not cost
    # a network round trip. Before `--dry-run` prints anything, so a preview cannot show a document
    # the real run would then reject, and before the schema, so it reads "country 'be' should be
    # 'BE'" rather than as a regex.
    assert_iso_codes_are_uppercase(
        {
            "metadata": {
                "title": arguments.url,
                "country": arguments.country,
                "subdivisions": arguments.subdivision,
            }
        }
    )
    document = build_catalog_document(
        download_feed_document(arguments.url),
        arguments.url,
        kind=arguments.kind,
        color=arguments.color,
        description=arguments.description,
        city=arguments.city,
        country=arguments.country,
        coverage=arguments.coverage,
        supportedLanguages=arguments.language,
        subdivisions=arguments.subdivision,
        publicationTypes=arguments.publication_type,
    )

    if arguments.dry_run:
        print(json.dumps(document, indent=2, ensure_ascii=False))
        return 0

    created = asyncio.run(add_catalog_from_url(arguments.url, document, Settings()))
    print(f"{'created' if created else 'updated'}: {document['metadata']['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
