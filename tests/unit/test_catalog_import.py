"""The pure half of `registry.cli.add`, no network, no database."""

import sys

import pytest

from registry.cli.__main__ import COMMANDS
from registry.cli.__main__ import main as cli_main
from registry.cli.add import (
    _HTTPOnlyRedirectHandler,
    assert_publicly_reachable,
    build_catalog_document,
    normalise_remote_rel,
)
from registry.core.errors import ValidationError
from registry.domain.enums import LinkRel

pytestmark = pytest.mark.unit

FEED_URL = "https://example.org/v1/api/opds"

OPDS_FEED = {
    "metadata": {"title": "Example Library", "numberOfItems": 0},
    "links": [
        {"href": "https://example.org/v1/api/opds?limit=50", "rel": "self"},
        {
            "href": "https://example.org/v1/api/opds/search{?query}",
            "rel": "search",
            "type": "application/opds+json",
            "templated": True,
        },
        {"href": "https://example.org/v1/api/shelf", "rel": "http://opds-spec.org/shelf"},
        {"href": "https://example.org/v1/api/opds?offset=50", "rel": "next"},
    ],
}


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("http://opds-spec.org/shelf", LinkRel.SHELF),  # OPDS 1.2 §6.1, the only alias
        ("shelf", LinkRel.SHELF),
        (["next", "search"], LinkRel.SEARCH),  # arrays are legal in link.schema.json
        ("self", None),  # , the registry synthesises its own
        ("catalog", None),  # supplied by the operator, never trusted from the feed
        ("next", None),
        # Defined by OPDS 1.2 §6.1 alongside `shelf`, and still not ours: the registry's
        # vocabulary is Hadrien's links table, not everything the specifications name.
        ("http://opds-spec.org/subscriptions", None),
        # No generic prefix rule, this must not become `icon`.
        ("http://opds-spec.org/image", None),
        (None, None),
    ],
)
def test_remote_rels_map_onto_the_registry_vocabulary(
    rel: object, expected: LinkRel | None
) -> None:
    assert normalise_remote_rel(rel) is expected


def test_the_operators_url_becomes_the_catalog_link() -> None:
    document = build_catalog_document(OPDS_FEED, FEED_URL, kind=["public"])

    catalog_links = [link for link in document["links"] if link["rel"] == "catalog"]
    assert catalog_links == [{"href": FEED_URL, "type": "application/opds+json", "rel": "catalog"}]
    # The feed's own `self` and paging links are not the registry's business.
    assert {link["rel"] for link in document["links"]} == {"catalog", "search", "shelf"}
    assert document["metadata"]["title"] == "Example Library"


def test_templated_survives_and_empty_editorial_fields_are_dropped() -> None:
    document = build_catalog_document(
        OPDS_FEED, FEED_URL, kind=["public"], color=None, supportedLanguages=[]
    )

    search = next(link for link in document["links"] if link["rel"] == "search")
    assert search["templated"] is True
    # `additionalProperties: false` on metadata means a null is not merely useless, it is
    # invalid, so absent flags must not become keys.
    assert set(document["metadata"]) == {"title", "kind"}


def test_a_feed_without_a_title_is_refused() -> None:
    with pytest.raises(ValidationError, match="nothing to name it"):
        build_catalog_document({"metadata": {}, "links": []}, FEED_URL, kind=["public"])


def test_a_deliberate_error_is_printed_as_a_message(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`make add` on a bad URL should say what is wrong, not print a stack trace."""
    monkeypatch.setattr(
        sys, "argv", ["registry.cli", "add", "file:///etc/passwd", "--kind", "open"]
    )

    assert cli_main() == 1
    assert "not an http(s) URL" in capsys.readouterr().err


def test_an_unreachable_database_explains_itself(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commonest failure is that the stack is down, which a traceback does not say."""
    monkeypatch.setattr(sys, "argv", ["registry.cli", "seed"])

    def refuse(_arguments: object) -> int:
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setitem(COMMANDS, "seed", refuse)

    assert cli_main() == 1
    reported = capsys.readouterr().err
    assert "Cannot reach the database" in reported
    assert "make up" in reported


def test_a_malformed_link_entry_does_not_crash_the_import() -> None:
    """`links: [null]` is valid JSON, and a remote document is untrusted."""
    feed = {
        "metadata": {"title": "Broken Links"},
        "links": [None, "not an object", {"href": "https://example.org/s", "rel": "shelf"}],
    }

    document = build_catalog_document(feed, FEED_URL, kind=["public"])

    assert {link["rel"] for link in document["links"]} == {"catalog", "shelf"}


def test_a_redirect_off_http_is_refused() -> None:
    """The scheme is checked on every hop. `urllib` blocks `file:` itself but allows `ftp:`."""
    handler = _HTTPOnlyRedirectHandler()

    with pytest.raises(ValidationError, match="refusing to follow a redirect"):
        handler.redirect_request(None, None, 302, "Found", {}, "ftp://example.org/passwd")


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",  # the cloud metadata service
        "http://127.0.0.1:8000/opds",
        "http://10.0.0.5/opds",
        "http://192.168.1.1/opds",
        "http://[::1]/opds",
    ],
)
def test_a_non_public_address_is_refused(url: str) -> None:
    """A catalog readers cannot reach is not a catalog, and this is the SSRF shape."""
    with pytest.raises(ValidationError, match="not a public address"):
        assert_publicly_reachable(url)


def test_a_redirect_to_a_private_address_is_refused() -> None:
    """A public host is free to redirect to a private one, so every hop is checked."""
    handler = _HTTPOnlyRedirectHandler()

    with pytest.raises(ValidationError, match="not a public address"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/")
