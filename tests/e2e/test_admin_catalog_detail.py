"""`GET /admin/catalogs/{id}`: one catalog in any status, for the detail page."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from registry.core.constants import PROBLEM_JSON_MEDIA_TYPE
from tests.conftest import admin_headers
from tests.e2e.test_admin_api import become, list_catalogs, set_status

pytestmark = pytest.mark.e2e


async def first_item(client: AsyncClient) -> dict[str, Any]:
    items: list[dict[str, Any]] = (await list_catalogs(client))["items"]
    return items[0]


async def read_detail(
    client: AsyncClient, catalog_id: str, email: str = "viewer@edrlab.org"
) -> Any:
    return await client.get(f"/admin/catalogs/{catalog_id}", headers=admin_headers(email))


async def test_the_detail_repeats_the_list_item_and_adds_the_rest(
    admin_client: AsyncClient, searchable_catalogs: None
) -> None:
    item = await first_item(admin_client)

    response = await read_detail(admin_client, item["id"])

    body = response.json()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert {key: body[key] for key in item} == item
    assert set(body) - set(item) == {
        "description",
        "publication_types",
        "subdivisions",
        "links",
        "created_at",
        "updated_at",
        "published_at",
        "submitter",
    }
    assert body["links"], "a seeded catalog has at least its catalog link"
    assert body["links"] == sorted(body["links"], key=lambda link: (link["rel"], link["href"]))
    assert set(body["links"][0]) == {"rel", "href", "type", "title", "templated", "authentication"}


async def test_subdivisions_carry_their_code_and_english_name(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    row = (
        await db_session.execute(
            text("SELECT catalog_id::text, subdivision_code FROM catalog_subdivisions LIMIT 1")
        )
    ).first()
    if row is None:
        pytest.skip("the seed has no catalog with a subdivision")

    body = (await read_detail(admin_client, row[0])).json()

    assert row[1] in [subdivision["code"] for subdivision in body["subdivisions"]]
    assert all(set(s) == {"code", "name"} for s in body["subdivisions"])


async def test_a_draft_is_readable_here_although_the_public_api_hides_it(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    item = await first_item(admin_client)
    await db_session.execute(
        text("UPDATE catalogs SET status = 'draft' WHERE id = CAST(:id AS uuid)"),
        {"id": item["id"]},
    )
    await db_session.commit()

    body = (await read_detail(admin_client, item["id"])).json()

    assert body["status"] == "draft"
    assert body["public_url"] is None
    assert (await admin_client.get(f"/catalogs/{item['id']}")).status_code == 404


@pytest.mark.parametrize(
    "catalog_id",
    ["00000000-0000-0000-0000-000000000000", "not-a-uuid", "1", "%00"],
    ids=["unknown", "word", "number", "null-byte"],
)
async def test_an_unknown_or_malformed_id_is_a_404_problem(
    admin_client: AsyncClient, catalog_id: str
) -> None:
    response = await read_detail(admin_client, catalog_id)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert response.json()["status"] == 404
    assert response.headers["cache-control"] == "no-store"


async def test_an_uppercase_id_finds_the_same_catalog(
    admin_client: AsyncClient, searchable_catalogs: None
) -> None:
    item = await first_item(admin_client)

    body = (await read_detail(admin_client, item["id"].upper())).json()

    assert body["id"] == item["id"]


async def test_only_a_reviewing_role_sees_the_submitter(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    item = await first_item(admin_client)
    await db_session.execute(
        text(
            "UPDATE catalogs SET submitter_name = 'Sam', submitter_email = 'sam@library.example'"
            " WHERE id = CAST(:id AS uuid)"
        ),
        {"id": item["id"]},
    )
    await db_session.commit()
    await become(db_session, "editor@edrlab.org", "editor")
    await become(db_session, "admin@edrlab.org", "admin")

    viewer = (await read_detail(admin_client, item["id"])).json()
    editor = (await read_detail(admin_client, item["id"], "editor@edrlab.org")).json()
    admin = (await read_detail(admin_client, item["id"], "admin@edrlab.org")).json()

    assert viewer["submitter"] is None
    assert (
        editor["submitter"]
        == admin["submitter"]
        == {
            "name": "Sam",
            "email": "sam@library.example",
        }
    )


async def test_a_catalog_without_a_submitter_has_none_even_for_an_editor(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    item = await first_item(admin_client)
    await become(db_session, "editor@edrlab.org", "editor")

    body = (await read_detail(admin_client, item["id"], "editor@edrlab.org")).json()

    assert body["submitter"] is None


async def test_the_detail_needs_credentials(admin_client: AsyncClient) -> None:
    response = await admin_client.get("/admin/catalogs/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 401


async def test_the_detail_is_one_statement(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    db_connection: AsyncConnection,
    searchable_catalogs: None,
) -> None:
    """R4: the actor login and the detail, nothing per link or subdivision."""
    item = await first_item(admin_client)
    await become(db_session, "count@edrlab.org", "viewer")
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if "SAVEPOINT" not in statement:
            seen.append(statement)

    event.listen(db_connection.sync_connection, "before_cursor_execute", record)
    try:
        response = await read_detail(admin_client, item["id"], "count@edrlab.org")
    finally:
        event.remove(db_connection.sync_connection, "before_cursor_execute", record)

    assert response.status_code == 200
    assert len(seen) == 2


async def test_an_editor_sees_the_actions_the_status_allows(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    item = await first_item(admin_client)
    await become(db_session, "editor@edrlab.org", "editor")
    await set_status(db_session, item["name"], "unpublished")

    body = (await read_detail(admin_client, item["id"], "editor@edrlab.org")).json()

    assert body["status"] == "unpublished"
    assert body["allowed_actions"]
    assert body["public_url"]
