"""Check a deployed registry end to end: the plumbing, then every documented search.

    make live-check                                      # http://localhost:8000
    make live-check URL=https://registry.thoriumreader.com
    make live-check URL=https://registry.thoriumreader.com ARGS="--deployed --max-ms 300"

Read-only: it only sends GET requests. Exit 0 when everything passed, 1 when a check failed, 2 when
the server does not answer.

Two parts, in this order:

1. **Plumbing.** Health, the top-level feed and its `search` link, the compression each kind of
   client should get, the security headers, and the error responses. This is the smoke test of the
   rollout runbook, run by a machine.
2. **Searches.** Every search of `tests/search_cases.py` is sent to `GET /search`, and the titles
   that come back must be exactly the ones the test-case page lists, in the same order: presence
   and position, per search (`docs/search-test-cases.md`). The score against the ideal answers is
   printed too, so a change that is better or worse shows as a number (`make search-score`).

The searches describe the twelve seed catalogs (`data/recommended.json` and `data/libraries.json`),
which is what production holds. Against a registry with other data, expect search failures.
"""

import argparse
import gzip
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import brotli
import zstandard

# Run as a file, `tests` is not importable until the repository root is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.search_quality import score_all, summarise, today_titles

TIMEOUT_SECONDS = 15
USER_AGENT = "registry-live-check"
MISSING_CATALOG = "00000000-0000-0000-0000-000000000000"
OK, UNPROCESSABLE, NOT_FOUND = 200, 422, 404

#: What each kind of client sends, and what it must get back (docs/performance.md).
COMPRESSION: tuple[tuple[str, str | None], ...] = (
    ("gzip, deflate, br, zstd", "zstd"),  # recent browsers
    ("gzip, deflate, br", "br"),  # Thorium Reader
    ("gzip, deflate", "gzip"),  # Apple, Android, Node, Python requests
    ("identity", None),  # KOReader: the body as it is
)


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]  # names lowercased
    body: bytes


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


#: One GET: path (with query string) and request headers in, the raw reply out.
Get = Callable[[str, dict[str, str]], Reply]


def fetch_response(base_url: str) -> Get:
    """A `Get` that talks to *base_url* over HTTP. A 4xx or 5xx is a reply, not an exception."""

    def get(path: str, headers: dict[str, str]) -> Reply:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}{path}", headers={"User-Agent": USER_AGENT, **headers}
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return Reply(
                    response.status,
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read(),
                )
        except urllib.error.HTTPError as error:
            return Reply(error.code, {k.lower(): v for k, v in error.headers.items()}, error.read())

    return get


def decode_body(reply: Reply) -> Any:
    """The JSON a reply carries, whatever coding it arrived in."""
    body = reply.body
    match reply.headers.get("content-encoding"):
        case "br":
            body = brotli.decompress(body)
        case "zstd":
            body = zstandard.ZstdDecompressor().decompress(body)
        case "gzip":
            body = gzip.decompress(body)
        case "deflate":
            body = zlib.decompress(body)
    return json.loads(body)


def search_path(query: str, page: int | None = None) -> str:
    params: dict[str, str] = {"query": query}
    if page is not None:
        params["page"] = str(page)
    return f"/search?{urllib.parse.urlencode(params)}"


def titles_of(reply: Reply) -> list[str]:
    return [c["metadata"]["title"] for c in decode_body(reply)["catalogs"]]


PLAIN = {"Accept-Encoding": "identity"}


def run_smoke_checks(get: Get, *, deployed: bool = False) -> list[Check]:
    """The plumbing a reader depends on, one named check each.

    *deployed* adds what must be true of a public service and is not true of a local run: the route
    that reads a library's feed on a visitor's behalf (`/dev/fetch`) must not exist.
    """
    checks: list[Check] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append(Check(name, ok, "" if ok else detail))

    ready = get("/health/ready", PLAIN)
    check("health is ready", ready.status == OK, f"HTTP {ready.status}")

    feed = get("/", PLAIN)
    check("the top-level feed answers", feed.status == OK, f"HTTP {feed.status}")
    if feed.status == OK:
        body = decode_body(feed)
        rels = {str(link.get("rel")) for link in body.get("links", [])}
        search_link: dict[str, Any] = next(
            (link for link in body.get("links", []) if link.get("rel") == "search"), {}
        )
        check("the feed has catalogs", bool(body.get("catalogs")), "it is empty")
        check("the feed advertises search", "search" in rels, f"links are {sorted(rels)}")
        check(
            "the search link is templated",
            bool(search_link.get("templated")) and "{?query}" in search_link.get("href", ""),
            str(search_link),
        )
        check(
            "the feed varies on Accept-Encoding",
            "accept-encoding" in feed.headers.get("vary", "").lower(),
            f"Vary is {feed.headers.get('vary')!r}",
        )
        check(
            "the feed is sent with nosniff",
            feed.headers.get("x-content-type-options") == "nosniff",
            "x-content-type-options is missing",
        )

    for offered, expected in COMPRESSION:
        reply = get("/", {"Accept-Encoding": offered})
        got = reply.headers.get("content-encoding")
        ok = reply.status == OK and got == expected
        check(
            f"Accept-Encoding {offered!r} gets {expected or 'the plain body'}",
            ok,
            f"got {got or 'plain'} (HTTP {reply.status})",
        )
        if ok:
            same = decode_body(reply) == decode_body(feed) if feed.status == OK else True
            check(f"the {expected or 'plain'} body decodes to the same feed", same, "it differs")

    empty = get(search_path(""), PLAIN)
    check(
        "an empty search is an empty feed",
        empty.status == OK and decode_body(empty)["metadata"]["numberOfItems"] == 0,
        f"HTTP {empty.status}",
    )
    beyond = get(search_path("paris", 0), PLAIN)
    check("page 0 is refused with 422", beyond.status == UNPROCESSABLE, f"HTTP {beyond.status}")
    console = get("/dev", PLAIN)
    check(
        "the console page answers",
        console.status == OK and b"Catalog Registry" in console.body,
        f"HTTP {console.status}",
    )
    if deployed:
        relay = get("/dev/fetch?url=https%3A%2F%2Fexample.org%2F", PLAIN)
        check(
            "/dev/fetch is not mounted on a deployed service",
            relay.status == NOT_FOUND,
            f"HTTP {relay.status}: it can make the server request a URL a visitor names",
        )
    missing = get(f"/catalogs/{MISSING_CATALOG}", PLAIN)
    check("an unknown catalog is a 404", missing.status == NOT_FOUND, f"HTTP {missing.status}")
    return checks


def run_search_checks(get: Get) -> tuple[list[Check], dict[str, list[str]]]:
    """Every documented search must return exactly the listed titles, in order."""
    from tests.search_cases import QUALITY_CASES  # noqa: PLC0415

    expected = today_titles()
    checks: list[Check] = []
    returned: dict[str, list[str]] = {}
    for case in QUALITY_CASES:
        reply = get(search_path(case.query), PLAIN)
        if reply.status != OK:
            returned[case.query] = []
            checks.append(Check(case.query, False, f"HTTP {reply.status}"))
            continue
        titles = titles_of(reply)
        returned[case.query] = titles
        ok = titles == expected[case.query]
        detail = "" if ok else f"wanted {expected[case.query]} got {titles}"
        checks.append(Check(case.query, ok, detail))
    return checks, returned


def median_search_ms(get: Get, runs: int = 5) -> float:
    durations = []
    for _ in range(runs):
        started = time.perf_counter()
        get(search_path("wallis"), {})
        durations.append((time.perf_counter() - started) * 1000)
    return statistics.median(durations)


def print_report(title: str, checks: list[Check], *, verbose: bool) -> int:
    failed = [c for c in checks if not c.ok]
    print(f"{title}: {len(checks) - len(failed)}/{len(checks)} passed")
    for c in checks if verbose else failed:
        print(
            f"  {'ok  ' if c.ok else 'FAIL'} {c.name}"
            + (f"\n         {c.detail}" if c.detail else "")
        )
    return len(failed)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://localhost:8000", help="the registry to check")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="list every check, not only failures"
    )
    parser.add_argument(
        "--deployed",
        action="store_true",
        help="also require what a public service must have: /dev/fetch is not mounted",
    )
    parser.add_argument(
        "--max-ms", type=float, help="fail when the median search takes longer than this (ms)"
    )
    args = parser.parse_args(argv)

    get = fetch_response(args.url)
    try:
        get("/health/live", PLAIN)
    except (urllib.error.URLError, OSError) as error:
        print(f"{args.url} is not answering ({error}). Is the server running (`make up`)?")
        return 2

    print(f"{args.url}\n")
    failures = print_report(
        "plumbing", run_smoke_checks(get, deployed=args.deployed), verbose=args.verbose
    )
    search_checks, returned = run_search_checks(get)
    print()
    failures += print_report("searches", search_checks, verbose=args.verbose)

    summary = summarise(score_all(returned))
    print(
        f"\nscore {summary.mean_score:.3f}   perfect {summary.perfect}/{summary.cases}   "
        f"recall {summary.mean_recall:.2f}   precision {summary.mean_precision:.2f}"
    )

    median = median_search_ms(get)
    print(f"median search time from here: {median:.0f} ms (it includes the distance to the server)")
    if args.max_ms is not None and median > args.max_ms:
        print(f"FAIL the median search took {median:.0f} ms, over the {args.max_ms:.0f} ms limit")
        failures += 1

    print("\nPASSED" if not failures else f"\nFAILED ({failures})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
