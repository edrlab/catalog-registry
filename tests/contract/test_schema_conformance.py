"""The response body, validated against the repository's own JSON Schemas.

Hadrien's stated use for those schemas (2026-08-18). Two things this catches that
nothing else does:

* `additionalProperties: false` on `metadata`, an internal column that reaches the response
  fails here immediately. The no-internal-fields rule, made executable.
* Schema drift. Hadrien edits the schemas independently; when he tightens a rule this goes
  red on the next run, so the service learns from CI rather than from a client bug report.
"""

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

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

    errors = sorted(build_schema_validator("feed.schema.json").iter_errors(response.json()))
    assert not errors, [error.message for error in errors]


async def test_every_catalog_validates_against_catalog_schema(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    response = await client.get("/")
    validator = build_schema_validator("catalog.schema.json")

    for catalog in response.json()["catalogs"]:
        errors = sorted(validator.iter_errors(catalog))
        assert not errors, [catalog["metadata"]["title"], [e.message for e in errors]]


async def test_an_empty_feed_still_validates(client: AsyncClient) -> None:
    """No recommended catalogs is a valid feed, not an error."""
    response = await client.get("/")

    assert response.json()["catalogs"] == []
    errors = sorted(build_schema_validator("feed.schema.json").iter_errors(response.json()))
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
