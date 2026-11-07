"""The feed check's pure rules: finding codes, reading a feed, a web page and a sign-in document."""

import json
import re

import pytest

from registry.domain.catalog_document import (
    MAX_DESCRIPTION_LENGTH,
    MAX_LINK_TITLE_LENGTH,
    MAX_MEDIA_TYPE_LENGTH,
    MAX_NAME_LENGTH,
)
from registry.domain.enums import LinkRel
from registry.domain.feed_check import (
    FINDINGS,
    MAX_JSON_DEPTH,
    MAX_QUOTED,
    Severity,
    Stage,
    build_feed_fields,
    build_feed_links,
    build_feed_shape_findings,
    build_finding,
    build_link_header_targets,
    build_page_fields,
    build_signed_in_fields,
    build_template_probe_url,
    is_authentication_document,
    normalise_remote_rel,
    parse_json_document,
    parse_markup_kind,
)

FEED_URL = "https://feeds.example.org/opds/"

#: The codes the admin API documents; the UI keys its wording on them, so the set must not drift.
CONTRACT_CODES = {
    "url-invalid", "url-unsafe", "host-unresolvable", "duplicate-catalog",
    "fetch-failed", "fetch-timed-out", "fetch-status", "fetch-too-large", "redirect-unsafe",
    "redirect-loop", "redirects-permanently", "redirects-temporarily", "https-downgrade",
    "content-type-unexpected", "not-json", "html-page", "atom-feed", "not-opds2",
    "title-missing", "self-missing", "no-collections", "feed-needs-sign-in", "link-dead",
    "link-needs-sign-in", "link-type-mismatch", "icon-not-image", "authenticate-wrong-type",
    "links-capped", "check-timed-out",
}  # fmt: skip


def test_the_codes_are_exactly_the_contracts() -> None:
    assert set(FINDINGS) == CONTRACT_CODES


def test_only_the_first_three_stages_block() -> None:
    blocking = {code for code, kind in FINDINGS.items() if kind.severity is Severity.ERROR}

    assert all(FINDINGS[code].stage is not Stage.LINKS for code in blocking)
    assert FINDINGS["html-page"].severity is Severity.INFO


@pytest.mark.parametrize("code", sorted(FINDINGS))
def test_every_code_reads_as_a_sentence(code: str) -> None:
    names = set(re.findall(r"\{(\w+)\}", FINDINGS[code].template)) - {"rel"}
    finding = build_finding(code, stage=Stage.LINKS, rel="search", **dict.fromkeys(names, "x"))

    assert finding.message
    assert "{" not in finding.message


def test_quoted_values_are_shortened() -> None:
    finding = build_finding("fetch-status", status="9" * 1000, url="https://x/" + "a" * 1000)

    assert len(finding.message) < MAX_QUOTED + 50
    assert finding.url is not None
    assert len(finding.url) == MAX_QUOTED


def test_an_unknown_code_fails_loudly() -> None:
    with pytest.raises(KeyError):
        build_finding("no-such-code")


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("search", LinkRel.SEARCH),
        ("http://opds-spec.org/shelf", LinkRel.SHELF),
        ("http://opds-spec.org/auth/document", LinkRel.AUTHENTICATE),
        (["start", "search"], LinkRel.SEARCH),
        ("self", None),
        ("catalog", None),
        ("next", None),
        ("http://opds-spec.org/subscriptions", None),
        (None, None),
        (42, None),
    ],
)
def test_remote_rels_map_to_the_registrys(rel: object, expected: LinkRel | None) -> None:
    assert normalise_remote_rel(rel) is expected


def test_links_are_absolute_known_http_and_unique() -> None:
    links = build_feed_links(
        [
            None,
            "text",
            {"href": "search{?query}", "rel": "search", "templated": True},
            {"href": "/shelf", "rel": "http://opds-spec.org/shelf"},
            {"href": "/shelf", "rel": "shelf"},
            {"href": "javascript:alert(1)", "rel": "icon"},
            {"href": "mailto:x@example.org", "rel": "alternate"},
            {"href": "https://cdn.example.org/i.png", "rel": "icon", "templated": True},
            {"href": "next?page=2", "rel": "next"},
            {"rel": "search"},
        ],
        FEED_URL,
    )

    assert [(link.rel, link.href, link.templated) for link in links] == [
        ("search", "https://feeds.example.org/opds/search{?query}", True),
        ("shelf", "https://feeds.example.org/shelf", False),
        ("icon", "https://cdn.example.org/i.png", False),
    ]
    assert all(link.active is None for link in links)


def test_a_feed_gives_its_title_description_languages_and_links() -> None:
    feed = {
        "metadata": {"title": "  A  Library ", "description": "Books.", "language": ["FR", "en"]},
        "links": [{"href": "/opds/search{?q}", "rel": "search", "templated": True}],
    }

    fields = build_feed_fields(feed, base_url=FEED_URL, address=FEED_URL)

    assert fields.name == "A Library"
    assert fields.description == "Books."
    assert fields.languages == ("fr", "en")
    assert [(link.rel, link.active) for link in fields.links] == [
        ("catalog", True),
        ("search", None),
    ]
    assert fields.links[0].href == FEED_URL


def test_suggested_text_fits_the_admin_api_limits() -> None:
    """A suggestion the editor saves unchanged must not be refused for its length."""
    long_link = {"rel": "search", "href": "/s", "type": "t" * 400, "title": "x" * 400}
    feed = {
        "metadata": {"title": "n" * 250, "description": "d" * 3000},
        "links": [long_link],
    }
    page = f"<html><head><title>{'n' * 250}</title>".encode() + (
        f'<meta name="description" content="{"d" * 3000}">'.encode()
    )
    authentication = {"title": "n" * 250, "description": "d" * 3000, "authentication": []}

    for fields in (
        build_feed_fields(feed, base_url=FEED_URL, address=FEED_URL),
        build_page_fields(page, base_url=FEED_URL, address=FEED_URL),
        build_signed_in_fields(authentication, address=FEED_URL, authentication_url=FEED_URL),
    ):
        assert len(fields.name or "") == MAX_NAME_LENGTH
        assert len(fields.description or "") == MAX_DESCRIPTION_LENGTH
    search = build_feed_fields(feed, base_url=FEED_URL, address=FEED_URL).links[1]
    assert len(search.type or "") == MAX_MEDIA_TYPE_LENGTH
    assert len(search.title or "") == MAX_LINK_TITLE_LENGTH


@pytest.mark.parametrize(
    ("document", "codes"),
    [
        ([], ["not-opds2"]),
        ({"links": []}, ["not-opds2"]),
        (
            {"metadata": {}, "links": [{"rel": "self", "href": "x"}], "publications": []},
            ["title-missing"],
        ),
        ({"metadata": {"title": "T"}, "publications": []}, ["not-opds2"]),
        ({"metadata": {"title": "T"}, "links": [], "navigation": []}, ["self-missing"]),
        (
            {"metadata": {"title": "T"}, "links": [{"rel": ["self"], "href": "x"}]},
            ["no-collections"],
        ),
        ({"metadata": {"title": "T"}, "links": [{"rel": "self", "href": "x"}], "groups": []}, []),
    ],
)
def test_feed_shape(document: object, codes: list[str]) -> None:
    assert [finding.code for finding in build_feed_shape_findings(document)] == codes


def test_deeply_nested_json_is_refused_before_parsing() -> None:
    bomb = b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1)

    with pytest.raises(ValueError, match="nested"):
        parse_json_document(bomb)


def test_brackets_inside_strings_do_not_count_as_depth() -> None:
    text = json.dumps({"title": "[" * 500 + '\\"{' * 100})

    assert parse_json_document(text.encode())["title"].startswith("[[[")


@pytest.mark.parametrize("body", [b"", b"{", b"\xff\xfe", b"NaN-ish", b"{'a': 1}"])
def test_invalid_json_is_a_value_error(body: bytes) -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_json_document(body)


@pytest.mark.parametrize(
    ("body", "media_type", "expected"),
    [
        (b"<!DOCTYPE html><html>", "", "html-page"),
        (b"\xef\xbb\xbf  <html lang=fr>", "", "html-page"),
        (b"{}", "text/html", "html-page"),
        (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">', "", "atom-feed"),
        (b"<feed>", "application/xml", "atom-feed"),
        (b"{}", "application/atom+xml;profile=opds-catalog", "atom-feed"),
        (b'{"metadata": {}}', "application/opds+json", None),
    ],
)
def test_markup_is_recognised(body: bytes, media_type: str, expected: str | None) -> None:
    assert parse_markup_kind(body, media_type.split(";", maxsplit=1)[0]) == expected


def test_a_web_page_gives_its_head() -> None:
    page = b"""<!doctype html><html lang="nl-BE"><head>
      <title> Bibliotheek &amp; Co </title>
      <meta name="Description" content="De bib.">
      <link rel="icon" href="/favicon.png">
      <script>document.title = "not this"</script>
    </head><body><title>Not this either</title></body></html>"""

    fields = build_page_fields(
        page, base_url="https://bib.example.org/", address="https://bib.example.org/"
    )

    assert fields.name == "Bibliotheek & Co"
    assert fields.description == "De bib."
    assert fields.languages == ("nl-be",)
    assert [(link.rel, link.href, link.type, link.active) for link in fields.links] == [
        ("catalog", "https://bib.example.org/", "text/html", True),
        ("icon", "https://bib.example.org/favicon.png", None, None),
    ]


def test_a_web_page_without_a_title_or_language_gives_none() -> None:
    fields = build_page_fields(
        b"<html lang='not a tag!'><body>x", base_url=FEED_URL, address=FEED_URL
    )

    assert fields.name is None
    assert fields.languages == ()
    assert len(fields.links) == 1


def test_a_sign_in_document_gives_the_name_and_both_links() -> None:
    document = {"id": "x", "title": "Members Library", "authentication": []}
    assert is_authentication_document(document)
    assert not is_authentication_document({"title": "x"})

    fields = build_signed_in_fields(
        document, address=FEED_URL, authentication_url="https://a.example/auth"
    )

    assert fields.name == "Members Library"
    assert [(link.rel, link.active) for link in fields.links] == [
        ("catalog", True),
        ("authenticate", True),
    ]


def test_link_headers_are_read() -> None:
    header = (
        '<https://x.example/a>; rel="next", </auth.json>; rel="http://opds-spec.org/auth/document"'
    )

    assert build_link_header_targets(header, "http://opds-spec.org/auth/document", FEED_URL) == [
        "https://feeds.example.org/auth.json"
    ]


@pytest.mark.parametrize(
    ("href", "expanded"),
    [
        ("https://x.example/search{?query}", "https://x.example/search"),
        ("https://x.example/s{?query,page}", "https://x.example/s"),
        ("https://x.example/{lang}/s", "https://x.example//s"),
        ("https://x.example/s", "https://x.example/s"),
        ("https://x.example/s{?query", None),
        ("https://x.example/s}", None),
    ],
)
def test_templates_expand_with_every_variable_undefined(href: str, expanded: str | None) -> None:
    assert build_template_probe_url(href) == expanded


def test_a_link_too_long_to_store_is_not_suggested() -> None:
    links = build_feed_links(
        [
            {"href": "https://example.org/" + "漢" * 1000, "rel": "search"},
            {"href": "https://example.org/shelf", "rel": "shelf"},
        ],
        "https://example.org/",
    )

    assert [link.rel for link in links] == ["shelf"]
