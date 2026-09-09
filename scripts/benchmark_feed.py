"""Measure `GET /` as the recommended set grows, with a different header per request.

    make bench              # 10, 100, 1000 catalogs
    make bench N="10 5000"  # any sizes

Q20 records that no latency target has ever been stated, so this asserts nothing and fails
nothing — `conventions/testing.md` §9 keeps performance out of the test suite deliberately,
because an invented threshold produces flaky builds and no information. This exists to answer
"what happens at n" when someone asks, and to give any future optimisation a before.

Synthetic rows are titled `ZZ Bench …` and deleted in a `finally` block, including on Ctrl-C.
Point `REGISTRY_DATABASE_URL` at a throwaway database: it writes and deletes rows.
"""

import asyncio
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

#: Real browser headers. Every user sends something different, which is the point — a cache
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


def measure(base_url: str, header: str | None) -> tuple[float, int]:
    """One request, over real HTTP. Returns (milliseconds, body size)."""
    request = urllib.request.Request(base_url)
    if header is not None:
        request.add_header("Accept-Language", header)
    started = time.perf_counter()
    with urllib.request.urlopen(request) as response:
        body = response.read()
    return (time.perf_counter() - started) * 1000, len(body)


def percentile(sorted_samples: list[float], fraction: float) -> float:
    return sorted_samples[min(int(len(sorted_samples) * fraction), len(sorted_samples) - 1)]


async def main(argv: list[str]) -> int:
    sizes = [int(value) for value in argv] or [10, 100, 1000]
    settings = Settings()
    base_url = f"{settings.base_url.rstrip('/')}/"

    try:
        measure(base_url, None)
    except urllib.error.URLError as error:
        print(f"{base_url} is not answering — is `make up` running? ({error})")
        return 1

    engine = create_database_engine(settings)
    print(f"{RUNS} requests per size, headers rotating, {base_url}\n")
    print(f"{'catalogs':>9} {'p50':>9} {'p95':>9} {'p99':>9} {'returned':>9} {'body':>9}")
    try:
        for count in sizes:
            await populate(engine, count)
            headers = itertools.cycle(HEADERS)
            for _ in range(WARMUP):
                measure(base_url, next(headers))

            samples, sizes_seen = [], []
            for _ in range(RUNS):
                elapsed, size = measure(base_url, next(headers))
                samples.append(elapsed)
                sizes_seen.append(size)
            samples.sort()

            with urllib.request.urlopen(base_url) as response:
                returned = json.loads(response.read())["metadata"]["numberOfItems"]

            print(
                f"{count:>9} {statistics.median(samples):8.1f}ms "
                f"{percentile(samples, 0.95):8.1f}ms {percentile(samples, 0.99):8.1f}ms "
                f"{returned:>9} {statistics.median(sizes_seen) / 1024:8.1f}K"
            )
    finally:
        async with engine.begin() as connection:
            await remove(connection)
        await engine.dispose()
        print(f"\n{MARKER} rows removed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
