"""The pure half of `registry.cli.add`, no network, no database."""

import pytest

from registry.cli.add import build_catalog_document, normalise_remote_rel
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
