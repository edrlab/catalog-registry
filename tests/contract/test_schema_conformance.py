"""The response body, validated against the repository's own JSON Schemas.

Hadrien's stated use for those schemas (2026-08-18). Two things this catches that
nothing else does:

* `additionalProperties: false` on `metadata`, an internal column that reaches the response
  fails here immediately. The no-internal-fields rule, made executable.
* Schema drift. Hadrien edits the schemas independently; when he tightens a rule this goes
  red on the next run, so the service learns from CI rather than from a client bug report.
"""

import json
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import import_catalog_document
from registry.core.config import Settings
from registry.core.constants import OPDS_CATALOG_MEDIA_TYPE
from registry.core.schema_validation import build_schema_validator
from registry.main import create_app

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]


async def test_the_top_level_feed_validates_against_feed_schema(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    response = await client.get("/")

    errors = sorted(
        build_schema_validator("feed.schema.json").iter_errors(response.json()), key=str
    )
    assert not errors, [error.message for error in errors]


async def test_every_catalog_validates_against_catalog_schema(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    response = await client.get("/")
    validator = build_schema_validator("catalog.schema.json")

    for catalog in response.json()["catalogs"]:
        errors = sorted(validator.iter_errors(catalog), key=str)
        assert not errors, [catalog["metadata"]["title"], [e.message for e in errors]]


async def test_the_rendered_identifier_is_the_catalog_id(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    """`metadata.identifier` is rendered from `catalogs.id`, not stored beside it.

    Asserted against the `self` link, which the renderer builds from the same id: if the two
    ever disagree, a client following `self` lands on a catalog whose identifier is not the
    one it just read.

    The hand-assigned values in `data/recommended.json` **are** what comes back, which is the
    point of ADR-038 and is asserted here against the file itself. Under ADR-037 they were read
    past and discarded, so an identifier Hadrien wrote down resolved to nothing.
    """
    response = await client.get("/")

    catalogs = response.json()["catalogs"]
    assert catalogs, "nothing was seeded, so this asserts nothing"
    seeded = json.loads((REPO_ROOT / "data" / "recommended.json").read_text(encoding="utf-8"))
    authored = {
        entry["metadata"]["title"]: entry["metadata"]["identifier"] for entry in seeded["catalogs"]
    }

    for catalog in catalogs:
        self_href = next(link["href"] for link in catalog["links"] if link["rel"] == "self")
        identifier = catalog["metadata"]["identifier"]
        assert identifier == f"urn:uuid:{self_href.rsplit('/', 1)[1]}"
        assert identifier == authored[catalog["metadata"]["title"]]


async def test_a_not_recommended_catalog_validates_against_catalog_schema(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """R7 for `GET /catalogs/{id}`, which no contract test reached before.

    Only the catalogs *inside the feed* were validated. A not-recommended catalog appears in no
    feed, so after ADR-038 an entire data set (`data/libraries.json`) is rendered exclusively by
    a path nothing schema-checked, including the `self` link this endpoint synthesises, which
    `catalog.schema.json` requires via its `contains` constraint.
    """
    href = "https://library.example/unlisted.opds2"
    document = {
        "metadata": {"title": "Unlisted Library", "kind": ["public"], "country": "be"},
        "links": [{"href": href, "rel": "catalog"}],
    }
    await import_catalog_document(db_session, document, recommended=False)
    await db_session.commit()

    response = await client.get(f"/catalogs/{uuid.uuid5(uuid.NAMESPACE_URL, href)}")

    assert response.status_code == 200
    errors = sorted(
        build_schema_validator("catalog.schema.json").iter_errors(response.json()), key=str
    )
    assert not errors, [error.message for error in errors]


async def test_an_empty_feed_still_validates(client: AsyncClient) -> None:
    """No recommended catalogs is a valid feed, not an error."""
    response = await client.get("/")

    assert response.json()["catalogs"] == []
    errors = sorted(
        build_schema_validator("feed.schema.json").iter_errors(response.json()), key=str
    )
    assert not errors, [error.message for error in errors]


@pytest.mark.parametrize(
    "path", sorted((REPO_ROOT / "demo" / "catalogs").glob("*.json")), ids=lambda p: p.name
)
def test_the_demo_fixtures_validate(path: Path) -> None:
    """If the fixtures and the schema disagree, one of them is wrong."""
    document = json.loads(path.read_text(encoding="utf-8"))

    errors = sorted(build_schema_validator("catalog.schema.json").iter_errors(document))
    assert not errors, [error.message for error in errors]


def test_the_openapi_document_describes_every_opds_response() -> None:
    """The response models exist to be published, so assert they actually are.

    A `JSONResponse` returned directly from a handler silently bypasses `response_model`,
    which is exactly the mistake this catches, the endpoint keeps working and the schema
    quietly goes empty.
    """
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/x",  # never connected to
        base_url="http://testserver",
    )
    spec = create_app(settings).openapi()

    for path, model in (("/", "FeedResponse"), ("/catalogs/{catalog_id}", "CatalogResponse")):
        content = spec["paths"][path]["get"]["responses"]["200"]["content"]
        assert OPDS_CATALOG_MEDIA_TYPE in content, f"{path} is not documented as OPDS"
        assert content[OPDS_CATALOG_MEDIA_TYPE]["schema"] == {
            "$ref": f"#/components/schemas/{model}"
        }
