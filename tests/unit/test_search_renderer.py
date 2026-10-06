"""Search feed links and paging, with no database: a `SearchPage` is a plain value."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from registry.core.constants import OPDS_JSON_MEDIA_TYPE
from registry.core.errors import RegistryError, SearchTimeoutError
from registry.rendering.search_renderer import (
    SEARCH_TITLE,
    build_search_template_link,
    render_search_results,
)
from registry.repositories.protocols import SearchPage

BASE = "https://example.org/"


def render(total: int, page: int, query: str = "paris") -> dict[str, Any]:
    return render_search_results(
        SearchPage(total=total, catalogs=()), query=query, page=page, page_size=50, base_url=BASE
    )


def rels(feed: dict[str, Any]) -> dict[str, str]:
    return {link["rel"]: link["href"] for link in feed["links"]}


def test_a_middle_page_links_both_ways() -> None:
    links = rels(render(total=120, page=2))

    assert links["previous"] == "https://example.org/search?query=paris"
    assert links["next"] == "https://example.org/search?query=paris&page=3"
    assert links["self"] == "https://example.org/search?query=paris&page=2"
    assert links["first"] == "https://example.org/search?query=paris"


def test_the_last_page_has_no_next_and_a_full_last_page_is_the_last() -> None:
    assert "next" not in rels(render(total=100, page=2))
    assert "next" in rels(render(total=101, page=2))


def test_the_first_page_has_no_previous() -> None:
    assert "previous" not in rels(render(total=500, page=1))


def test_the_query_is_url_encoded_and_the_empty_query_is_kept() -> None:
    assert rels(render(0, 1, "a&b=c d"))["self"].endswith("?query=a%26b%3Dc%20d")
    assert rels(render(0, 1, ""))["self"] == "https://example.org/search?query="


def test_metadata_reports_the_total_even_when_the_page_is_empty() -> None:
    assert render(total=7, page=9)["metadata"] == {
        "title": "Search results",
        "numberOfItems": 7,
        "itemsPerPage": 50,
        "currentPage": 9,
    }


# More edge cases: page-size boundaries, awkward query text, and the shape of the links.


def render_sized(total: int, page: int, page_size: int) -> dict[str, object]:
    return render_search_results(
        SearchPage(total=total, catalogs=()),
        query="paris",
        page=page,
        page_size=page_size,
        base_url=BASE,
    )


@pytest.mark.parametrize(
    ("total", "page", "page_size", "has_next"),
    [
        (0, 1, 50, False),
        (1, 1, 50, False),
        (49, 1, 50, False),
        (50, 1, 50, False),  # exactly one full page: nothing after it
        (51, 1, 50, True),
        (100, 2, 50, False),
        (101, 2, 50, True),
        (1, 1, 1, False),
        (2, 1, 1, True),
        (2, 2, 1, False),
        (50_000, 1000, 50, False),  # the last page the route allows, and it is the last
        # `next` stops at MAX_PAGE even when matches remain: page 1001 is a 422.
        (50_001, 1000, 50, False),
        (5, 4, 2, False),  # past the end: no next
        (5, 3, 2, False),
        (5, 2, 2, True),
    ],
)
def test_next_is_present_exactly_when_matches_remain(
    total: int, page: int, page_size: int, has_next: bool
) -> None:
    assert ("next" in rels(render_sized(total, page, page_size))) is has_next


@pytest.mark.parametrize(("page", "has_previous"), [(1, False), (2, True), (9, True), (1000, True)])
def test_previous_is_present_exactly_when_the_page_is_not_the_first(
    page: int, has_previous: bool
) -> None:
    assert ("previous" in rels(render_sized(10, page, 50))) is has_previous


def test_self_search_and_first_are_always_present() -> None:
    for total, page in [(0, 1), (0, 5), (500, 1), (500, 10)]:
        assert {"self", "search", "first"} <= set(rels(render_sized(total, page, 50)))


def test_a_page_past_the_end_still_reports_the_total_and_its_own_number() -> None:
    feed = render_sized(total=6, page=4, page_size=2)

    assert feed["metadata"]["numberOfItems"] == 6  # type: ignore[index]
    assert feed["metadata"]["currentPage"] == 4  # type: ignore[index]
    assert feed["metadata"]["itemsPerPage"] == 2  # type: ignore[index]
    assert feed["catalogs"] == []


@pytest.mark.parametrize(
    "query",
    [
        "a&b",
        "a=b",
        "a#b",
        "100%",
        "%41",
        "a+b",
        "a b",
        "&=#%+",
        "?x=1&y=2",
        "paris OR valais",
        '"numérique de paris"',
        "-paris",
        "Suiße",
        "日本語",
        "📚 libraries",
        "café́",
        "a/b\\c",
        "<script>alert(1)</script>",
        "line\nbreak",
        "tab\there",
        "'; DROP TABLE catalogs; --",
    ],
)
def test_the_query_survives_the_link_unchanged(query: str) -> None:
    """Parse each link back and the original text must come out, whatever it contained."""
    feed = render(total=500, page=2, query=query)

    for rel in ("self", "first", "previous", "next"):
        href = rels(feed)[rel]
        assert parse_qs(urlsplit(href).query, keep_blank_values=True)["query"] == [query], rel
        assert "#" not in href and " " not in href


def test_special_characters_are_percent_encoded_not_form_encoded() -> None:
    """`+` must be `%2B` and a space `%20`: a `+` would read back as a space in some clients."""
    href = rels(render(0, 1, "a+b c&d=e#f%g"))["self"]

    assert href.endswith("?query=a%2Bb%20c%26d%3De%23f%25g")


def test_unicode_is_utf8_percent_encoded() -> None:
    assert rels(render(0, 1, "Suiße 📚"))["self"].endswith("?query=Sui%C3%9Fe%20%F0%9F%93%9A")


def test_page_one_is_the_plain_query_url_and_later_pages_add_page_last() -> None:
    links = rels(render(total=500, page=3, query="a&b"))

    assert links["first"] == "https://example.org/search?query=a%26b"
    assert links["self"] == "https://example.org/search?query=a%26b&page=3"
    assert links["previous"] == "https://example.org/search?query=a%26b&page=2"
    assert links["next"] == "https://example.org/search?query=a%26b&page=4"


def test_a_base_url_with_or_without_a_trailing_slash_gives_the_same_links() -> None:
    page = SearchPage(total=0, catalogs=())
    with_slash = render_search_results(
        page, query="x", page=1, page_size=50, base_url="https://example.org/"
    )
    without = render_search_results(
        page, query="x", page=1, page_size=50, base_url="https://example.org"
    )

    assert with_slash == without


def test_the_template_link_is_a_templated_search_link() -> None:
    assert build_search_template_link("https://example.org/") == {
        "href": "https://example.org/search{?query}",
        "type": OPDS_JSON_MEDIA_TYPE,
        "rel": "search",
        "templated": True,
    }


def test_every_link_carries_the_opds_media_type() -> None:
    for link in render(total=500, page=2)["links"]:
        assert link["type"] == OPDS_JSON_MEDIA_TYPE


def test_the_feed_title_is_search_results() -> None:
    assert render(0, 1)["metadata"]["title"] == SEARCH_TITLE == "Search results"


def test_the_empty_query_renders_a_feed_with_an_empty_query_in_every_link() -> None:
    links = rels(render(0, 1, ""))

    assert links["self"] == links["first"] == "https://example.org/search?query="


def test_a_search_timeout_is_a_503_service_unavailable_registry_error() -> None:
    error = SearchTimeoutError("took too long")

    assert isinstance(error, RegistryError)
    assert (error.status_code, error.title) == (503, "Service Unavailable")
    assert str(error) == "took too long"
