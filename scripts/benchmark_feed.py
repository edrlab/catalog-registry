"""Measure `GET /` and `GET /search` as the catalog set grows.

    make bench              # 10, 100, 1000 catalogs
    make bench N="10 5000"  # any sizes

`GET /` is timed with a different `Accept-Language` per request. `GET /search` is timed with a
rotating mix of queries (a title word, a place name in another language, a typo, a phrase, a
negation) over synthetic catalogs that have a country and varied titles, so each query matches a
realistic share of them rather than all. Hadrien's target for search is under 100 ms warm from
Europe, 150 ms acceptable; the script prints it beside the numbers and still asserts nothing.

Asserts nothing and cannot fail the build: no latency target has ever been agreed, and an
invented threshold produces flaky builds and no information.

**It writes.** Synthetic rows are titled `ZZ Bench …` and deleted in a `finally`; point
`REGISTRY_DATABASE_URL` at a throwaway database.
"""

import asyncio
import gzip
import itertools
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from sqlalchemy import text

from registry.core.config import Settings
from registry.db.session import create_database_engine

#: Titles are prefixed with this so cleanup can find them without touching real data.
MARKER = "ZZ Bench"

#: Sorts after every realistic title, so synthetic rows never displace real ones in the
#: ordering being measured.
assert MARKER > "Z"

#: Language mix modelled on a real registry: regional variants, siblings that must not match
#: each other, multi-language catalogs, and catalogs scoped to no language at all.
LANGUAGE_MIX = [
    [],
    [],
    ["en"],
    ["en-us"],
    ["en-gb"],
    ["en-in"],
    ["en-ca"],
    ["en-au"],
    ["fr"],
    ["fr-be"],
    ["fr-ca"],
    ["de"],
    ["de-at"],
    ["es"],
    ["ja"],
    ["en", "fr"],
    ["en", "de", "fr"],
    ["nl"],
    ["pt-br"],
    ["it"],
]

#: Real browser headers. Every user sends something different, which is the point, a cache
#: keyed on the raw header would treat all of these as distinct.
HEADERS = [
    "en-US,en;q=0.9",
    "en-US,en-IN;q=0.9,en-GB;q=0.8,en;q=0.7",
    "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "de-DE,de;q=0.9,en;q=0.8",
    "ja,en-US;q=0.9,en;q=0.8",
    "pt-BR,pt;q=0.9,es;q=0.8,en;q=0.7",
    "en-GB,en;q=0.9,fr;q=0.8,de;q=0.7,es;q=0.6,it;q=0.5",
    "*",
    None,
]

WARMUP, RUNS = 20, 120

#: Title words and countries cycled over the synthetic catalogs, so a search term matches a
#: share of them instead of every row. Coprime lengths (23, 7) spread the combinations.
TITLE_WORDS = [
    "Alder", "Birch", "Cedar", "Dune", "Elm", "Fjord", "Glade", "Heath", "Isle", "Juniper",
    "Kestrel", "Lagoon", "Meadow", "Nettle", "Orchard", "Pine", "Quarry", "Ridge", "Spruce",
    "Thistle", "Upland", "Valley", "Willow",
]  # fmt: skip
COUNTRIES = ["FR", "BE", "CH", "CA", "DE", "IT", "ES"]

#: What a reader plausibly types. Each is (label, query).
SEARCH_QUERIES = [
    ("title word", "willow"),
    ("two words", "alder valley"),
    ("place, other language", "belgique"),
    ("place, German", "schweiz"),
    ("typo, place name", "belgum"),
    ("phrase", '"cedar fjord"'),
    ("negation", "pine -france"),
    ("no match", "zzzzqqq"),
]

#: Hadrien, 1 October: under 100 ms warm from Europe, 150 ms acceptable.
TARGET_MS = 100


async def populate(engine, count: int) -> None:
    """Replace the synthetic rows with *count* fresh ones. Real rows are untouched."""
    async with engine.begin() as connection:
        await remove(connection)
        for index in range(count):
            catalog_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO catalogs"
                    " (id, status, recommended, title, color, country_code, published_at)"
                    " VALUES (:id, 'active', true, :title, 'gray', :country, now())"
                ),
                {
                    "id": catalog_id,
                    "title": (
                        f"{MARKER} {TITLE_WORDS[index % len(TITLE_WORDS)]} "
                        f"{TITLE_WORDS[(index // len(TITLE_WORDS)) % len(TITLE_WORDS)]} {index:05}"
                    ),
                    "country": COUNTRIES[index % len(COUNTRIES)],
                },
            )
            await connection.execute(
                text("INSERT INTO catalog_kinds VALUES (:id, 'open')"), {"id": catalog_id}
            )
            await connection.execute(
                text("INSERT INTO catalog_publication_types VALUES (:id, 'ebook')"),
                {"id": catalog_id},
            )
            for tag in LANGUAGE_MIX[index % len(LANGUAGE_MIX)]:
                await connection.execute(
                    text("INSERT INTO catalog_languages VALUES (:id, :tag)"),
                    {"id": catalog_id, "tag": tag},
                )
            for position, rel in enumerate(("catalog", "alternate", "icon")):
                await connection.execute(
                    text(
                        "INSERT INTO links (id, catalog_id, href, rel)"
                        " VALUES (gen_random_uuid(), :id, :href, :rel)"
                    ),
                    {
                        "id": catalog_id,
                        "href": f"https://bench.example/{index}/{position}",
                        "rel": rel,
                    },
                )


async def analyse(engine) -> None:
    """What `VACUUM ANALYZE catalog_search` is for after a bulk import (docs/search.md)."""
    async with engine.begin() as connection:
        await connection.execute(text("ANALYZE catalog_search"))


async def remove(connection) -> None:
    await connection.execute(
        text("DELETE FROM catalogs WHERE title LIKE :marker"), {"marker": f"{MARKER}%"}
    )


def measure(base_url: str, header: str | None) -> tuple[float, int, int]:
    """One request, over real HTTP. Returns (milliseconds, bytes on the wire, bytes decoded).

    `Accept-Encoding: gzip` because every real client sends it, and measuring without it
    reports a payload nobody receives. `urllib` does not add it on its own.
    """
    request = urllib.request.Request(base_url, headers={"Accept-Encoding": "gzip"})
    if header is not None:
        request.add_header("Accept-Language", header)
    started = time.perf_counter()
    with urllib.request.urlopen(request) as response:
        body = response.read()
        compressed = response.headers.get("Content-Encoding") == "gzip"
    elapsed = (time.perf_counter() - started) * 1000
    decoded = len(gzip.decompress(body)) if compressed else len(body)
    return elapsed, len(body), decoded


def measure_search(base_url: str, query: str) -> tuple[float, int]:
    """One search over real HTTP. Returns (milliseconds, `numberOfItems`)."""
    url = f"{base_url}search?{urllib.parse.urlencode({'query': query})}"
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    started = time.perf_counter()
    with urllib.request.urlopen(request) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    elapsed = (time.perf_counter() - started) * 1000
    return elapsed, json.loads(body)["metadata"]["numberOfItems"]


def report_search(base_url: str, count: int) -> None:
    print(f"\n  /search at {count} synthetic catalogs (target p95 under {TARGET_MS} ms)")
    print(f"  {'query':<24} {'p50':>9} {'p95':>9} {'matches':>9}")
    for label, query in SEARCH_QUERIES:
        for _ in range(WARMUP):
            measure_search(base_url, query)
        samples, matches = [], 0
        for _ in range(RUNS):
            elapsed, matches = measure_search(base_url, query)
            samples.append(elapsed)
        cuts = statistics.quantiles(samples, n=100, method="inclusive")
        print(f"  {label:<24} {statistics.median(samples):8.1f}ms {cuts[94]:8.1f}ms {matches:>9}")


async def main(argv: list[str]) -> int:
    sizes = [int(value) for value in argv] or [10, 100, 1000]
    settings = Settings()
    base_url = f"{settings.base_url.rstrip('/')}/"

    try:
        measure(base_url, None)
    except urllib.error.URLError as error:
        print(f"{base_url} is not answering. Is `make up` running? ({error})")
        return 1

    engine = create_database_engine(settings)
    print(f"{RUNS} requests per size, headers rotating, {base_url}\n")
    print(
        f"{'catalogs':>9} {'p50':>9} {'p95':>9} {'p99':>9} {'returned':>9} {'wire':>8} {'raw':>8}"
    )
    try:
        for count in sizes:
            await populate(engine, count)
            await analyse(engine)
            headers = itertools.cycle(HEADERS)
            for _ in range(WARMUP):
                measure(base_url, next(headers))

            samples, wire, raw = [], [], []
            for _ in range(RUNS):
                elapsed, on_wire, decoded = measure(base_url, next(headers))
                samples.append(elapsed)
                wire.append(on_wire)
                raw.append(decoded)
            cuts = statistics.quantiles(samples, n=100, method="inclusive")

            with urllib.request.urlopen(base_url) as response:
                returned = json.loads(response.read())["metadata"]["numberOfItems"]

            print(
                f"{count:>9} {statistics.median(samples):8.1f}ms "
                f"{cuts[94]:8.1f}ms {cuts[98]:8.1f}ms "
                f"{returned:>9} {statistics.median(wire) / 1024:7.1f}K "
                f"{statistics.median(raw) / 1024:7.1f}K"
            )
            report_search(base_url, count)
    finally:
        async with engine.begin() as connection:
            await remove(connection)
        await engine.dispose()
        print(f"\n{MARKER} rows removed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
