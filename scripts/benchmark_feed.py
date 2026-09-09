"""Measure `GET /` as the recommended set grows, with a different header per request.

    make bench              # 10, 100, 1000 catalogs
    make bench N="10 5000"  # any sizes

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


async def populate(engine, count: int) -> None:
    """Replace the synthetic rows with *count* fresh ones. Real rows are untouched."""
    async with engine.begin() as connection:
        await remove(connection)
        for index in range(count):
            catalog_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO catalogs (id, status, recommended, title, color, published_at)"
                    " VALUES (:id, 'active', true, :title, 'gray', now())"
                ),
                {"id": catalog_id, "title": f"{MARKER} {index:05}"},
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
    finally:
        async with engine.begin() as connection:
            await remove(connection)
        await engine.dispose()
        print(f"\n{MARKER} rows removed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
