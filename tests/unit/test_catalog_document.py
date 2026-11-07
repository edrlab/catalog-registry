"""The checks on one catalog document that the seed, `add` and the admin API share."""

import uuid
from typing import Any

import pytest

from registry.cli.seed import (
    assert_document_is_consistent,
    assert_iso_codes_are_uppercase,
    resolve_catalog_id,
)
from registry.core.errors import ValidationError
from registry.domain.catalog_document import (
    build_catalog_id,
    build_document_problems,
    build_identity_href,
)

HREF = "https://example.org/opds.json"
#: 2006 characters, 6020 bytes in UTF-8: over the href index's limit though under 2048 characters.
WIDE_HREF = "https://example.org/" + "漢" * 2000


def document(links: list[dict[str, Any]] | None = None, **metadata: Any) -> dict[str, Any]:
    return {
        "metadata": {"title": "T", "kind": ["public"]} | metadata,
        "links": links if links is not None else [{"rel": "catalog", "href": HREF}],
    }


def problems(doc: dict[str, Any]) -> list[tuple[tuple[str | int, ...], str]]:
    return [(problem.path, problem.message) for problem in build_document_problems(doc)]


def test_a_valid_document_has_no_problems() -> None:
    doc = document(
        [
            {"rel": "catalog", "href": HREF},
            {"rel": ["shelf", "start"], "href": "https://example.org/shelf"},
            {"rel": "search", "href": "https://example.org/s{?query}", "templated": True},
            {"rel": "icon", "href": HREF},
        ],
        country="BE",
        subdivisions=["BE-WAL"],
        supportedLanguages=["fr", "nl-BE"],
        publicationTypes=["ebook", "audiobook"],
    )

    assert problems(doc) == []


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        (document(country="be"), [(("metadata", "country"), "country 'be' should be 'BE'")]),
        (
            document(subdivisions=["BE-WAL", "Wallonia"]),
            [
                (
                    ("metadata", "subdivisions", 1),
                    "subdivisions 'Wallonia' is not a well-formed ISO code",
                )
            ],
        ),
        (
            document(kind=["public", "open", "public"]),
            [(("metadata", "kind", 2), "kind 'public' is listed twice")],
        ),
        (
            document(supportedLanguages=["pt-BR", "pt-br"]),
            [(("metadata", "supportedLanguages", 1), "language 'pt-br' is listed twice")],
        ),
        (
            document([{"rel": "icon", "href": HREF}]),
            [(("links",), "At least one catalog or shelf link is required")],
        ),
        (
            document([{"rel": "catalog", "href": "javascript:alert(1)"}]),
            [(("links", 0, "href"), "Must be an http or https URL")],
        ),
        (
            document([{"rel": "catalog", "href": WIDE_HREF}]),
            [(("links", 0, "href"), "The address is too long.")],
        ),
        (
            document([{"rel": "catalog", "href": "https://example.org/" + "a" * 2028}]),
            [],
        ),
        (
            document(city=" Liège "),
            [(("metadata", "city"), "Remove the spaces at the start or end.")],
        ),
        (
            document(city="A" * 101),
            [(("metadata", "city"), "Use at most 100 characters.")],
        ),
        (
            document(city="Liège "),
            [(("metadata", "city"), "Remove the spaces at the start or end.")],
        ),
        (
            document([{"rel": "catalog", "href": HREF, "templated": True}]),
            [(("links", 0, "templated"), "Only a search link can be templated")],
        ),
        (
            document([{"rel": "catalog", "href": HREF}, {"rel": "shelf", "href": HREF}]),
            [
                (
                    ("links", 1, "href"),
                    f"The shelf link and the catalog link cannot share the href {HREF}",
                )
            ],
        ),
        (
            document(
                [{"rel": "catalog", "href": HREF}, {"rel": "catalog", "href": HREF, "title": "x"}]
            ),
            [(("links", 1, "href"), f"A catalog link to {HREF} is listed twice")],
        ),
    ],
)
def test_each_problem_names_its_place(
    doc: dict[str, Any], expected: list[tuple[tuple[str | int, ...], str]]
) -> None:
    assert problems(doc) == expected


@pytest.mark.parametrize(
    "doc",
    [
        document(kind=["public", "public"]),
        document(supportedLanguages=["fr", "FR"]),
        document([{"rel": "catalog", "href": "ftp://example.org"}]),
        document([{"rel": "catalog", "href": HREF, "templated": True}]),
        document([{"rel": "catalog", "href": HREF}, {"rel": "shelf", "href": HREF}]),
        document([{"rel": "catalog", "href": WIDE_HREF}]),
        document(city="Paris 75"),
        document(city="Paris "),
    ],
)
def test_the_seed_refuses_what_the_admin_api_refuses(doc: dict[str, Any]) -> None:
    """One rule set: every consistency problem the admin API reports stops the seed, with the
    same sentence, before anything reaches the database."""
    with pytest.raises(ValidationError) as raised:
        assert_document_is_consistent(doc)

    for _, message in problems(doc):
        assert message in str(raised.value)


def test_the_seed_still_explains_an_iso_code_in_one_sentence() -> None:
    with pytest.raises(
        ValidationError, match=r"T has invalid ISO codes: country 'be' should be 'BE'\."
    ):
        assert_iso_codes_are_uppercase(document(country="be"))


def test_the_admin_api_and_the_seed_derive_the_same_id() -> None:
    doc = document([{"rel": "shelf", "href": "https://x"}, {"rel": "catalog", "href": HREF}])

    assert build_identity_href(doc) == HREF
    assert build_catalog_id(HREF) == uuid.uuid5(uuid.NAMESPACE_URL, HREF)
    assert resolve_catalog_id(doc, HREF) == build_catalog_id(HREF)
