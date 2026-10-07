"""The live check must pass on a healthy registry and name what is wrong on a broken one.

A checker that cannot fail is worth nothing, so every failure mode here is simulated and must be
caught by name. The fake registry compresses with the real `choose_encoding` and the real codecs, so
the plumbing checks run against bytes that look like the real thing.
"""

import gzip
import json
import urllib.parse
from collections.abc import Callable
from typing import Any

import brotli
import live_check
import pytest
import zstandard
from live_check import Check, Reply

from registry.api.compression import choose_encoding
from tests.search_cases import QUALITY_CASES
from tests.search_quality import today_titles

PADDING = "A public library with many catalogs. " * 40  # over 1 KB, so it gets compressed


def feed_body(*, search_link: bool = True, catalogs: int = 3) -> dict[str, Any]:
    links: list[dict[str, Any]] = [{"rel": "self", "href": "http://x/"}]
    if search_link:
        links.append({"rel": "search", "href": "http://x/search{?query}", "templated": True})
    return {
        "metadata": {"title": "Registry"},
        "links": links,
        "catalogs": [
            {"metadata": {"title": f"C{i}", "description": PADDING}} for i in range(catalogs)
        ],
    }


def answer(
    body: dict[str, Any], offered: str | None, *, status: int = 200, vary: bool = True
) -> Reply:
    raw = json.dumps(body).encode()
    headers = {"content-type": "application/opds+json", "x-content-type-options": "nosniff"}
    if vary:
        headers["vary"] = "Accept-Encoding"
    coding = choose_encoding(offered) if len(raw) >= 1024 else None
    if coding == "zstd":
        raw = zstandard.ZstdCompressor(level=6).compress(raw)
    elif coding == "br":
        raw = brotli.compress(raw, quality=4)
    elif coding == "gzip":
        raw = gzip.compress(raw, mtime=0)
    if coding:
        headers["content-encoding"] = coding
    return Reply(status, headers, raw)


class FakeRegistry:
    """A registry that behaves, with switches for each way it can misbehave."""

    def __init__(self) -> None:
        self.titles = today_titles()
        self.search_link = True
        self.compress = True
        self.ready = 200
        self.vary = True
        self.edit: Callable[[str, list[str]], list[str]] = lambda query, titles: titles

    def __call__(self, path: str, headers: dict[str, str]) -> Reply:
        offered = headers.get("Accept-Encoding") if self.compress else "identity"
        url = urllib.parse.urlsplit(path)
        params = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        if url.path == "/health/ready":
            return answer({"status": "ready"}, offered, status=self.ready)
        if url.path == "/":
            return answer(feed_body(search_link=self.search_link), offered, vary=self.vary)
        if url.path.startswith("/catalogs/"):
            return answer({"title": "Not Found"}, offered, status=404)
        if url.path == "/search":
            if params.get("page") == ["0"]:
                return answer({"title": "Unprocessable"}, offered, status=422)
            query = params.get("query", [""])[0]
            titles = self.edit(query, list(self.titles.get(query, [])))
            body = {
                "metadata": {"numberOfItems": len(titles)},
                "links": [],
                "catalogs": [{"metadata": {"title": t}} for t in titles],
            }
            return answer(body, offered)
        return answer({}, offered, status=404)


def failed(checks: list[Check]) -> list[str]:
    return [c.name for c in checks if not c.ok]


def test_a_healthy_registry_passes_everything() -> None:
    registry = FakeRegistry()

    assert failed(live_check.run_smoke_checks(registry)) == []
    checks, returned = live_check.run_search_checks(registry)
    assert failed(checks) == []
    assert len(checks) == len(QUALITY_CASES)
    some = next(q for q, titles in registry.titles.items() if titles)
    assert returned[some] == registry.titles[some]


@pytest.mark.parametrize(
    ("break_it", "named"),
    [
        (lambda r: setattr(r, "search_link", False), "the feed advertises search"),
        (lambda r: setattr(r, "compress", False), "Accept-Encoding 'gzip, deflate, br' gets br"),
        (lambda r: setattr(r, "ready", 503), "health is ready"),
        (lambda r: setattr(r, "vary", False), "the feed varies on Accept-Encoding"),
    ],
)
def test_each_plumbing_failure_is_named(
    break_it: Callable[[FakeRegistry], None], named: str
) -> None:
    registry = FakeRegistry()
    break_it(registry)

    assert named in failed(live_check.run_smoke_checks(registry))


def test_a_search_in_the_wrong_order_is_caught_by_position() -> None:
    registry = FakeRegistry()
    query = next(q for q, titles in registry.titles.items() if len(titles) >= 2)
    registry.edit = lambda q, titles: titles[::-1] if q == query else titles

    checks, _ = live_check.run_search_checks(registry)

    assert failed(checks) == [query]
    assert "wanted" in next(c.detail for c in checks if c.name == query)


def test_a_missing_result_and_an_extra_result_are_both_caught() -> None:
    registry = FakeRegistry()
    queries = [q for q, titles in registry.titles.items() if titles][:2]
    registry.edit = lambda q, titles: (
        titles[1:] if q == queries[0] else [*titles, "Intruder"] if q == queries[1] else titles
    )

    checks, _ = live_check.run_search_checks(registry)

    assert set(failed(checks)) == set(queries)


def test_a_server_error_on_a_search_is_a_failure_not_a_crash() -> None:
    registry = FakeRegistry()
    target = QUALITY_CASES[0].query

    def broken(path: str, headers: dict[str, str]) -> Reply:
        if path == live_check.search_path(target):
            return Reply(500, {}, b"")
        return registry(path, headers)

    checks, returned = live_check.run_search_checks(broken)

    assert failed(checks) == [target]
    assert returned[target] == []
