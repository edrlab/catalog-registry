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
import ipaddress
import json
import socket
import sys
import urllib.request
from collections.abc import Sequence
from typing import Any, override
from urllib.parse import urlsplit

from registry.cli.seed import import_feed_document
from registry.core.config import Settings
from registry.core.errors import ValidationError
from registry.db.session import build_session_factory, create_database_engine
from registry.domain.enums import (
    CatalogColor,
    CatalogKind,
    CoverageScope,
    LinkRel,
    PublicationType,
)

#: Checked on the URL given and again on every redirect hop.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: A registry entry is metadata, not a mirror. Twelve seconds and two megabytes is a generous
#: ceiling for a feed's first page, and it bounds a hostile or broken origin.
FETCH_TIMEOUT_SECONDS = 12
MAX_FEED_BYTES = 2 * 1024 * 1024

#: The one alias the specifications actually define for a rel this registry stores.
#:
#: OPDS 1.2 §6.1 defines the relation as `http://opds-spec.org/shelf` and gives it no short
#: form; the bare `shelf` is the registry's own vocabulary, transcribed from Hadrien's links
#: table (`scripts/generate_enums.py`). OPDS 2.0 §4 does define short aliases for the OPDS
#: 1.x URIs, but only for the acquisition relations, none of which the registry stores, and
#: `shelf` does not appear in OPDS 2.0 at all.
#:
#: So this is a table, not a prefix rule. Stripping `http://opds-spec.org/` generically would
#: silently invent short forms the specifications never defined. `subscriptions`, `facet`,
#: `crawlable`, `recommended`, and import them as though Hadrien had listed them.
REL_ALIASES = {"http://opds-spec.org/shelf": LinkRel.SHELF}

#: Never imported from a feed: the registry synthesises its own `self`, and
#: `catalog` is the address the operator gave, not one the feed claims for itself.
NEVER_IMPORTED = {LinkRel.SELF, LinkRel.CATALOG}


def assert_publicly_reachable(url: str) -> None:
    """Refuse a URL no reader could follow. Raises `ValidationError`.

    Two things at once, and the second is why this exists. A catalog in a public registry has
    to be reachable from the public internet, so a loopback or private address is not a
    catalog anyone can use. And an address that is not globally routable is the shape of an
    SSRF target: `169.254.169.254` is the cloud metadata service, which hands credentials to
    anything that asks. Harmless while this runs on a laptop, not harmless the day it runs
    inside GCP, and that is a change nobody would think to re-audit this for.

    Checked on the URL given and again on every redirect hop, because a public host is free
    to redirect to a private one.
    """
    host = urlsplit(url).hostname
    if not host:
        raise ValidationError(f"{url} names no host")
    try:
        resolved = socket.getaddrinfo(host, None)
    except OSError as error:
        raise ValidationError(f"could not resolve {host}. {error}") from error

    for entry in resolved:
        address = ipaddress.ip_address(entry[4][0])
        if not address.is_global:
            raise ValidationError(
                f"{host} resolves to {address}, which is not a public address. A catalog in "
                "the registry has to be reachable by readers."
            )


class _HTTPOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-check the scheme on every hop, not just the first.

    `urllib` already refuses to redirect to `file:`, but it permits `ftp:`, and a public host
    is free to redirect to a private one. Checking the URL the operator typed is not the same
    as checking where the request ends up.
    """

    @override
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        if urlsplit(newurl).scheme not in ALLOWED_SCHEMES:
            raise ValidationError(f"refusing to follow a redirect to {newurl}")
        assert_publicly_reachable(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_only_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_HTTPOnlyRedirectHandler)


def download_feed_document(url: str) -> dict[str, Any]:
    """Fetch and parse an OPDS feed. Raises `ValidationError` on anything unusable.

    Scheme and destination are checked before the request. `urlopen` also speaks `file:` and
    `ftp:`, so an unchecked URL turns this command into a local-file reader, and an unchecked
    host turns it into a way to read whatever the machine can reach.
    """
    if urlsplit(url).scheme not in ALLOWED_SCHEMES:
        raise ValidationError(f"{url} is not an http(s) URL")
    assert_publicly_reachable(url)

    request = urllib.request.Request(
        url, headers={"Accept": "application/opds+json, application/json"}
    )
    try:
        with _http_only_opener().open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = response.read(MAX_FEED_BYTES + 1)
    except OSError as error:  # URLError, HTTPError, socket timeouts
        raise ValidationError(f"could not fetch {url}. {error}") from error

    if len(payload) > MAX_FEED_BYTES:
        raise ValidationError(f"{url} returned more than {MAX_FEED_BYTES} bytes")

    try:
        feed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValidationError(f"{url} did not return JSON. {error}") from error

    if not isinstance(feed, dict):
        raise ValidationError(f"{url} returned {type(feed).__name__}, not an OPDS document")
    return feed


def normalise_remote_rel(rel: Any) -> LinkRel | None:
    """One OPDS rel → the registry's vocabulary, or `None` for one we do not store.

    A Readium link's `rel` is a string *or* an array (`link.schema.json`), so both are
    accepted; the first recognised value in an array wins.

    Anything outside `LinkRel` and `REL_ALIASES` returns `None`. Including relations the
    OPDS specifications do define, such as `http://opds-spec.org/subscriptions`. The registry
    stores the eight rels in Hadrien's links table and no others; a feed cannot widen that by
    publishing something else, whether it is malformed or merely broader than we support.
    """
    candidates = rel if isinstance(rel, list) else [rel]
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        if (aliased := REL_ALIASES.get(candidate)) is not None:
            return aliased
        try:
            resolved = LinkRel(candidate)
        except ValueError:
            continue
        if resolved not in NEVER_IMPORTED:
            return resolved
    return None


def build_catalog_document(feed: dict[str, Any], url: str, **editorial: Any) -> dict[str, Any]:
    """The seed-shaped catalog document for *feed*, fetched from *url*.

    *url* becomes the `catalog` rel, the operator asked for this address, and it is the
    identity the upsert matches on. Trusting the feed's own `self` instead would let a
    remote rename split one catalog into two rows.

    `editorial` holds the flag-supplied metadata; keys whose value is `None` or empty are
    dropped, so `metadata` never carries a null that `additionalProperties: false` would
    otherwise have to allow.
    """
    title = feed.get("metadata", {}).get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValidationError(f"{url} has no metadata.title, so there is nothing to name it")

    links = [{"href": url, "type": "application/opds+json", "rel": LinkRel.CATALOG.value}]
    for link in feed.get("links", []):
        # A remote document is untrusted, and `links: [null]` is valid JSON. Reaching `.get`
        # on it raises before the schema ever sees the document.
        if not isinstance(link, dict):
            continue
        resolved = normalise_remote_rel(link.get("rel"))
        href = link.get("href")
        if resolved is None or not isinstance(href, str):
            continue
        imported: dict[str, Any] = {"href": href, "rel": resolved.value}
        if isinstance(link.get("type"), str):
            imported["type"] = link["type"]
        if link.get("templated"):
            imported["templated"] = True
        links.append(imported)

    metadata: dict[str, Any] = {"title": title.strip()}
    metadata.update({key: value for key, value in editorial.items() if value})
    return {"metadata": metadata, "links": links}


async def add_catalog_from_url(url: str, document: dict[str, Any], settings: Settings) -> bool:
    """Upsert one catalog document. Returns True if it was created, False if it replaced one."""
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
    parser.add_argument("--country", help="ISO 3166-1 alpha-2")
    parser.add_argument("--coverage", choices=[scope.value for scope in CoverageScope])
    parser.add_argument("--language", action="append", help="BCP-47; repeatable")
    parser.add_argument("--subdivision", action="append", help="ISO 3166-2; repeatable")
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
