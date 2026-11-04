"""`POST /admin/catalogs/{id}/transitions` and `PUT /admin/catalogs/{id}/recommended`."""

from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.core.constants import PROBLEM_JSON_MEDIA_TYPE
from registry.domain.lifecycle import TRANSITION_ACTIONS
from tests.conftest import admin_headers
from tests.e2e.test_admin_api import become, list_catalogs

pytestmark = pytest.mark.e2e

EDITOR = "editor@edrlab.org"
PROBLEMS = "https://registry.thoriumreader.com/problems/"
MISSING = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def editor(db_session: AsyncSession) -> str:
    await become(db_session, EDITOR, "editor")
    return EDITOR


async def a_catalog(client: AsyncClient, db_session: AsyncSession, status: str) -> dict[str, Any]:
    """A seeded catalog put in *status*, as the detail page would read it."""
    catalog_id = (await list_catalogs(client))["items"][0]["id"]
    await db_session.execute(
        text(
            "UPDATE catalogs SET status = CAST(:s AS catalog_status), published_at = now() "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"s": status, "id": catalog_id},
    )
    await db_session.commit()
    response = await client.get(f"/admin/catalogs/{catalog_id}", headers=admin_headers(EDITOR))
    detail: dict[str, Any] = response.json()
    return detail


async def transition(
    client: AsyncClient, catalog_id: str, body: Any, email: str = EDITOR
) -> Response:
    return await client.post(
        f"/admin/catalogs/{catalog_id}/transitions", json=body, headers=admin_headers(email)
    )


async def recommend(
    client: AsyncClient, catalog_id: str, body: Any, email: str = EDITOR
) -> Response:
    return await client.put(
        f"/admin/catalogs/{catalog_id}/recommended", json=body, headers=admin_headers(email)
    )


def problem_type(response: Response) -> str:
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    kind: str = response.json()["type"]
    return kind.removeprefix(PROBLEMS)


async def status_and_version(db_session: AsyncSession, catalog_id: str) -> tuple[str, int]:
    row = (
        await db_session.execute(
            text("SELECT status::text, version FROM catalogs WHERE id = CAST(:id AS uuid)"),
            {"id": catalog_id},
        )
    ).one()
    return row[0], row[1]


# --- transitions ---------------------------------------------------------------------------------


async def test_publishing_a_draft_makes_it_public_and_bumps_the_version(
    admin_client: AsyncClient, db_session: AsyncSession, editor: str, searchable_catalogs: None
) -> None:
    draft = await a_catalog(admin_client, db_session, "draft")

    response = await transition(
        admin_client, draft["id"], {"action": "publish", "version": draft["version"]}
    )

    body = response.json()
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert body["undo_action"] is None, "nothing returns a catalog to draft"
    assert body["catalog"]["status"] == "published"
    assert body["catalog"]["version"] == draft["version"] + 1
    assert body["catalog"]["published_at"]
    assert "unpublish" in body["catalog"]["allowed_actions"]
    assert (await admin_client.get(f"/catalogs/{draft['id']}")).status_code == 200


async def test_unpublishing_hides_it_from_search_and_offers_publish_as_the_undo(
    admin_client: AsyncClient, db_session: AsyncSession, editor: str, searchable_catalogs: None
) -> None:
    published = await a_catalog(admin_client, db_session, "published")

    response = await transition(
        admin_client, published["id"], {"action": "unpublish", "version": published["version"]}
    )

    assert response.status_code == 200, response.text
    assert response.json()["undo_action"] == "publish"
    searchable = await db_session.scalar(
        text("SELECT count(*) FROM catalog_search WHERE catalog_id = CAST(:id AS uuid)"),
        {"id": published["id"]},
    )
    assert searchable == 0, "the search trigger follows the status"
    assert (await admin_client.get(f"/catalogs/{published['id']}")).status_code == 200, (
        "an unpublished catalog keeps its direct link"
    )


async def test_unarchiving_lands_on_unpublished_with_no_undo(
    admin_client: AsyncClient, db_session: AsyncSession, editor: str, searchable_catalogs: None
) -> None:
    archived = await a_catalog(admin_client, db_session, "archived")

    body = (
        await transition(
            admin_client, archived["id"], {"action": "unarchive", "version": archived["version"]}
        )
    ).json()

    assert body["catalog"]["status"] == "unpublished"
    assert body["undo_action"] is None


async def test_a_stale_version_is_a_version_conflict_and_changes_nothing(
    admin_client: AsyncClient, db_session: AsyncSession, editor: str, searchable_catalogs: None
) -> None:
    published = await a_catalog(admin_client, db_session, "published")
    before = await status_and_version(db_session, published["id"])

    response = await transition(
        admin_client,
        published["id"],
        {"action": "unpublish", "version": published["version"] + 1},
    )

    assert response.status_code == 409
    assert problem_type(response) == "version-conflict"
    assert await status_and_version(db_session, published["id"]) == before


async def test_a_transition_the_lifecycle_refuses_is_an_invalid_transition(
    admin_client: AsyncClient, db_session: AsyncSession, editor: str, searchable_catalogs: None
) -> None:
    draft = await a_catalog(admin_client, db_session, "draft")

    response = await transition(
        admin_client, draft["id"], {"action": "archive", "version": draft["version"]}
    )

    assert response.status_code == 409
    assert problem_type(response) == "invalid-transition"


async def test_two_writes_from_the_same_version_apply_once(
    admin_client: AsyncClient, db_session: AsyncSession, editor: str, searchable_catalogs: None
) -> None:
    published = await a_catalog(admin_client, db_session, "published")
    seen = {"action": "unpublish", "version": published["version"]}

    first = await transition(admin_client, published["id"], seen)
    second = await transition(admin_client, published["id"], seen)

    assert (first.status_code, second.status_code) == (200, 409)
    assert problem_type(second) == "version-conflict"


# --- recommended ---------------------------------------------------------------------------------


async def test_a_published_catalog_can_be_recommended_and_unrecommended(
    admin_client: AsyncClient, db_session: AsyncSession, editor: str, searchable_catalogs: None
) -> None:
    published = await a_catalog(admin_client, db_session, "published")

    on = await recommend(
        admin_client, published["id"], {"recommended": True, "version": published["version"]}
    )
    off = await recommend(
        admin_client, published["id"], {"recommended": False, "version": on.json()["version"]}
    )

    assert on.status_code == 200, on.text
    assert on.json()["recommended"] is True
    assert off.json()["recommended"] is False
    assert off.json()["version"] == published["version"] + 2


@pytest.mark.parametrize("status", ["draft", "unpublished", "archived"])
async def test_only_a_published_catalog_changes_its_recommendation(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    editor: str,
    searchable_catalogs: None,
    status: str,
) -> None:
    catalog = await a_catalog(admin_client, db_session, status)

    response = await recommend(
        admin_client, catalog["id"], {"recommended": True, "version": catalog["version"]}
    )

    assert response.status_code == 409
    assert problem_type(response) == "not-published"


async def test_a_stale_recommendation_is_a_version_conflict(
    admin_client: AsyncClient, db_session: AsyncSession, editor: str, searchable_catalogs: None
) -> None:
    published = await a_catalog(admin_client, db_session, "published")

    response = await recommend(
        admin_client, published["id"], {"recommended": True, "version": published["version"] + 5}
    )

    assert response.status_code == 409
    assert problem_type(response) == "version-conflict"


# --- who, what, which ----------------------------------------------------------------------------


async def test_a_viewer_can_write_nothing(
    admin_client: AsyncClient, db_session: AsyncSession, editor: str, searchable_catalogs: None
) -> None:
    published = await a_catalog(admin_client, db_session, "published")
    before = await status_and_version(db_session, published["id"])
    seen = published["version"]

    responses = [
        await transition(
            admin_client, published["id"], {"action": action, "version": seen}, "v@edrlab.org"
        )
        for action in TRANSITION_ACTIONS
    ] + [
        await recommend(
            admin_client, published["id"], {"recommended": True, "version": seen}, "v@edrlab.org"
        )
    ]

    assert [r.status_code for r in responses] == [403] * 5
    assert await status_and_version(db_session, published["id"]) == before


@pytest.mark.parametrize("catalog_id", [MISSING, "not-a-uuid"])
async def test_an_unknown_catalog_is_catalog_not_found_on_reads_and_writes(
    admin_client: AsyncClient, editor: str, catalog_id: str
) -> None:
    responses = [
        await admin_client.get(f"/admin/catalogs/{catalog_id}", headers=admin_headers(EDITOR)),
        await transition(admin_client, catalog_id, {"action": "publish", "version": 1}),
        await recommend(admin_client, catalog_id, {"recommended": True, "version": 1}),
    ]

    assert [r.status_code for r in responses] == [404] * 3
    assert {problem_type(r) for r in responses} == {"catalog-not-found"}


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"action": "publish"}, "version"),
        ({"action": "publish", "version": "1"}, "version"),
        ({"action": "publish", "version": 0}, "version"),
        ({"action": "publish", "version": True}, "version"),
        ({"action": "delete", "version": 1}, "action"),
        ({"action": "recommend", "version": 1}, "action"),
        ({"action": "publish", "version": 1, "status": "published"}, "status"),
    ],
)
async def test_a_malformed_transition_is_a_422_naming_the_field(
    admin_client: AsyncClient, editor: str, body: dict[str, Any], field: str
) -> None:
    response = await transition(admin_client, MISSING, body)

    assert response.status_code == 422
    assert problem_type(response) == "validation"
    assert field in [error["field"] for error in response.json()["errors"]]


async def test_a_recommendation_must_be_a_real_boolean(
    admin_client: AsyncClient, editor: str
) -> None:
    response = await recommend(admin_client, MISSING, {"recommended": "yes", "version": 1})

    assert response.status_code == 422
    assert [error["field"] for error in response.json()["errors"]] == ["recommended"]


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
@pytest.mark.parametrize("status", ["draft", "published", "unpublished", "archived"])
async def test_allowed_actions_are_exactly_what_the_endpoints_accept(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    editor: str,
    searchable_catalogs: None,
    *,
    role: str,
    status: str,
) -> None:
    """Every status and role: an action in `allowed_actions` succeeds, any other is refused."""
    email = f"{role}-check@edrlab.org"
    await become(db_session, email, role)

    for action in [*TRANSITION_ACTIONS, "recommend"]:
        catalog = await a_catalog(admin_client, db_session, status)
        offered = (
            action
            in (
                await admin_client.get(
                    f"/admin/catalogs/{catalog['id']}", headers=admin_headers(email)
                )
            ).json()["allowed_actions"]
        )
        if action == "recommend":
            body = {"recommended": True, "version": catalog["version"]}
            response = await recommend(admin_client, catalog["id"], body, email)
        else:
            body = {"action": action, "version": catalog["version"]}
            response = await transition(admin_client, catalog["id"], body, email)

        assert (response.status_code == 200) == offered, (status, role, action, response.text)
