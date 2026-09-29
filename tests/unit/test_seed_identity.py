"""Identity resolution and the derived seed-input schema. Pure, no database.

These were briefly in `tests/integration/test_seed.py`, which was wrong: the `integration`
marker means "requires a database" (`pyproject.toml`) and `conventions/testing.md` puts pure
logic here. `test_only_a_browsable_rel_gives_a_document_an_identity` is the parametrised table
that used to cover `has_browsable_rel` in `tests/unit/test_links.py`, before
`resolve_identity_href` absorbed that job. It belongs in the same millisecond lane it started in.
"""

import uuid

import pytest

from registry.cli.seed import SEED_INPUT_SCHEMA, resolve_catalog_id, resolve_identity_href
from registry.core.errors import ValidationError
from registry.core.schema_validation import build_schema_validator

pytestmark = pytest.mark.unit


def test_the_published_schema_still_demands_uppercase() -> None:
    """Uppercase is the one accepted form, on input and on output alike.

    `schema/catalog.schema.json` is upstream's contract and was never edited by this branch. The
    input schema is generated from it and does not relax the pattern either, so this assertion and
    `test_lowercase_codes_are_rejected_on_input` are the same rule seen from both ends."""
    published = build_schema_validator("catalog.schema.json")
    document = {
        "metadata": {
            "title": "Lowercase",
            "identifier": "urn:uuid:30a59158-28bc-4fc1-ad99-93325968d9c1",
            "kind": ["public"],
            "country": "be",
        },
        "links": [
            {"href": "https://library.example/self", "rel": "self"},
            {"href": "https://library.example/home.opds2", "rel": "catalog"},
        ],
    }

    messages = [error.message for error in published.iter_errors(document)]

    assert any("'be' does not match" in message for message in messages)


@pytest.mark.parametrize(
    ("rels", "identified"),
    [
        (["catalog"], True),
        (["shelf"], True),
        (["catalog", "icon"], True),
        (["icon"], False),
        (["alternate"], False),
        # `self` is synthesised, so it is neither required nor sufficient on input.
        (["self"], False),
    ],
)
def test_only_a_browsable_rel_gives_a_document_an_identity(
    rels: list[str], identified: bool
) -> None:
    """The rel table `resolve_identity_href` is the single gate for.

    It used to be `has_browsable_rel` in `domain/links.py`, which asked the same question one
    layer away and could answer it differently. One function decides now, and this is its
    table.
    """
    document = {
        "metadata": {"title": "Example"},
        "links": [{"href": f"https://library.example/{rel}", "rel": rel} for rel in rels],
    }

    if identified:
        assert resolve_identity_href(document).startswith("https://library.example/")
    else:
        with pytest.raises(ValidationError, match="no stable identity"):
            resolve_identity_href(document)


def test_an_absent_identifier_is_derived_from_the_identity_href() -> None:
    """`uuid5(NAMESPACE_URL, href)`, so the id is the same wherever the seed is run.

    `data/libraries.json` carries no identifiers. Letting Postgres generate one would make the
    id local to whichever database ran the seed, and those catalogs are not recommended, so no
    feed would ever disclose it.
    """
    document = {"metadata": {"title": "No Identifier"}, "links": []}

    derived = resolve_catalog_id(document, "https://www.lirtuel.be/v1/home.opds2")

    assert derived == uuid.uuid5(uuid.NAMESPACE_URL, "https://www.lirtuel.be/v1/home.opds2")
    assert derived == resolve_catalog_id(document, "https://www.lirtuel.be/v1/home.opds2")
    assert derived != resolve_catalog_id(document, "https://www.lirtuel.be/v1/other.opds2")


def test_a_supplied_identifier_wins_over_the_derived_one() -> None:
    document = {
        "metadata": {"title": "Both", "identifier": "urn:uuid:8e0de3da-0d9d-4b5f-bb2f-bd9b3ba4a7c5"}
    }

    assert str(resolve_catalog_id(document, "https://example.org/opds")) == (
        "8e0de3da-0d9d-4b5f-bb2f-bd9b3ba4a7c5"
    )


def test_lowercase_codes_are_rejected_on_input() -> None:
    """The generated input schema keeps the published patterns, so lowercase is rejected.

    One canonical form, enforced at import rather than folded. The rejection is asserted here as
    well as in the integration test, because this is the layer that produces the message the
    author reads.
    """
    feed = {
        "metadata": {"title": "Libraries"},
        "catalogs": [
            {
                "metadata": {
                    "title": "Lowercase",
                    "kind": ["public"],
                    "country": "be",
                    "subdivisions": ["be-wal"],
                },
                "links": [{"href": "https://library.example/home.opds2", "rel": "catalog"}],
            }
        ],
    }

    messages = [
        error.message for error in build_schema_validator(SEED_INPUT_SCHEMA).iter_errors(feed)
    ]

    assert any("'be' does not match" in message for message in messages)
    assert any("'be-wal' does not match" in message for message in messages)


def test_uppercase_codes_are_accepted_on_input() -> None:
    """The other half, so the rejection above cannot pass by rejecting everything."""
    feed = {
        "metadata": {"title": "Libraries"},
        "catalogs": [
            {
                "metadata": {
                    "title": "Uppercase",
                    "kind": ["public"],
                    "country": "BE",
                    "subdivisions": ["BE-WAL"],
                },
                "links": [{"href": "https://library.example/home.opds2", "rel": "catalog"}],
            }
        ],
    }

    assert not list(build_schema_validator(SEED_INPUT_SCHEMA).iter_errors(feed))


def test_the_seed_input_schema_does_not_require_an_identifier() -> None:
    """`data/libraries.json` supplies none, and the registry answers the field itself.

    `schema/catalog.schema.json` still requires it, because every *rendered* catalog has one.
    The relaxation belongs to the input schema only, alongside the `self` relaxations.
    """
    feed = {
        "metadata": {"title": "Libraries"},
        "catalogs": [
            {
                "metadata": {"title": "No Identifier", "kind": ["public"]},
                "links": [{"href": "https://library.example/home.opds2", "rel": "catalog"}],
            }
        ],
    }

    assert not list(build_schema_validator(SEED_INPUT_SCHEMA).iter_errors(feed))


@pytest.mark.parametrize(
    "identifier",
    [
        pytest.param("urn:uuid:not-a-uuid", id="not-a-uuid"),
        pytest.param("urn:uuid:", id="prefix-only"),
    ],
)
def test_a_malformed_identifier_is_a_validation_error(identifier: str) -> None:
    """`uuid.UUID` raises `ValueError`, which is not a `RegistryError`, so it escaped the CLI as
    a traceback naming no catalog. The schema's pattern blocks both today, but
    `resolve_catalog_id` is a public entry point and is called directly from the tests."""
    document = {"metadata": {"title": "Bad Identifier", "identifier": identifier}}

    with pytest.raises(ValidationError, match="Bad Identifier"):
        resolve_catalog_id(document, "https://library.example/home.opds2")


def test_an_uppercase_urn_prefix_is_the_same_identifier() -> None:
    """RFC 9562 §4: the `urn:uuid:` prefix is case-insensitive."""
    value = "30a59158-28bc-4fc1-ad99-93325968d9c1"
    lower = {"metadata": {"title": "x", "identifier": f"urn:uuid:{value}"}}
    upper = {"metadata": {"title": "x", "identifier": f"URN:UUID:{value.upper()}"}}
    href = "https://library.example/home.opds2"

    assert resolve_catalog_id(lower, href) == resolve_catalog_id(upper, href)


def test_the_derived_id_is_pinned_to_a_known_value() -> None:
    """A golden value, because every other test restates the formula.

    Changing the namespace, or normalising the href, would keep those green while moving every
    id a client has written down, the one property the derivation exists to provide.
    """
    document = {"metadata": {"title": "Lirtuel"}}

    assert resolve_catalog_id(document, "https://www.lirtuel.be/v1/home.opds2") == uuid.UUID(
        "6fb7a1fb-087f-54ef-95fb-9ffe0c87c012"
    )
