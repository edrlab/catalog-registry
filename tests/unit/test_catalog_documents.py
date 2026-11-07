"""`build_document_from_catalog`: the inverse of `build_catalog`, which an edit starts from."""

import uuid

from registry.repositories.catalog_documents import build_catalog, build_document_from_catalog


def test_every_link_attribute_survives_the_round_trip() -> None:
    """An edit that does not touch the links must not drop `type`, `title` or `templated`."""
    links = [
        {"rel": "catalog", "href": "https://example.org/opds.json", "title": "Home"},
        {
            "rel": "search",
            "href": "https://example.org/search{?query}",
            "type": "application/opds+json",
            "templated": True,
        },
    ]
    document = {"metadata": {"title": "T", "kind": ["public"]}, "links": links}

    catalog = build_catalog(document, uuid.uuid4(), recommended=False)

    assert build_document_from_catalog(catalog)["links"] == links
