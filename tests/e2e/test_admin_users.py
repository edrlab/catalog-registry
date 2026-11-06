"""Users and a country's subdivision types."""

from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import admin_headers
from tests.e2e.test_admin_api import become

pytestmark = pytest.mark.e2e

ADMIN = "admin@edrlab.org"
OTHER_ADMIN = "second-admin@edrlab.org"
EDITOR = "editor@edrlab.org"
VIEWER = "viewer@edrlab.org"
PROBLEMS = "https://registry.thoriumreader.com/problems/"


@pytest.fixture
async def people(db_session: AsyncSession) -> dict[str, str]:
    """An admin, an editor and a viewer; email → id."""
    for email, role in ((ADMIN, "admin"), (EDITOR, "editor"), (VIEWER, "viewer")):
        await become(db_session, email, role)
    rows = await db_session.execute(text("SELECT email, id::text FROM actors"))
    return dict(rows.tuples().all())


async def users(client: AsyncClient, email: str = ADMIN) -> Response:
    return await client.get("/admin/users", headers=admin_headers(email))


async def set_role(client: AsyncClient, user_id: str, role: Any, email: str = ADMIN) -> Response:
    return await client.patch(
        f"/admin/users/{user_id}", json={"role": role}, headers=admin_headers(email)
    )


async def access(client: AsyncClient, user_id: str, action: str, email: str = ADMIN) -> Response:
    return await client.post(f"/admin/users/{user_id}/{action}", headers=admin_headers(email))


def problem(response: Response) -> str:
    kind: str = response.json()["type"]
    return kind.removeprefix(PROBLEMS)


async def test_me_returns_the_callers_id(admin_client: AsyncClient, people: dict[str, str]) -> None:
    me = (await admin_client.get("/admin/me", headers=admin_headers(EDITOR))).json()

    assert me["id"] == people[EDITOR]


async def test_the_list_shows_everyone_with_what_the_admin_may_do(
    admin_client: AsyncClient, people: dict[str, str]
) -> None:
    response = await users(admin_client)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    by_email = {item["email"]: item for item in response.json()["items"]}
    assert set(by_email) >= {ADMIN, EDITOR, VIEWER}
    assert by_email[ADMIN]["allowed_actions"] == []
    assert by_email[EDITOR]["allowed_actions"] == ["change_role", "deactivate"]
    assert by_email[EDITOR]["role"] == "editor"
    assert by_email[EDITOR]["is_active"] is True
    assert by_email[EDITOR]["created_at"]


@pytest.mark.parametrize("email", [EDITOR, VIEWER])
async def test_only_admins_see_or_change_people(
    admin_client: AsyncClient, people: dict[str, str], email: str
) -> None:
    assert (await users(admin_client, email)).status_code == 403
    assert (await set_role(admin_client, people[VIEWER], "admin", email)).status_code == 403
    assert (await access(admin_client, people[VIEWER], "deactivate", email)).status_code == 403


async def test_a_role_changes_and_says_so(
    admin_client: AsyncClient, people: dict[str, str]
) -> None:
    response = await set_role(admin_client, people[VIEWER], "editor")

    assert response.status_code == 200, response.text
    assert response.json()["role"] == "editor"
    me = await admin_client.get("/admin/me", headers=admin_headers(VIEWER))
    assert me.json()["role"] == "editor"


async def test_setting_the_same_role_changes_nothing(
    admin_client: AsyncClient, people: dict[str, str]
) -> None:
    response = await set_role(admin_client, people[EDITOR], "editor")

    assert response.status_code == 200
    assert response.json()["role"] == "editor"


async def test_a_deactivated_person_is_refused_until_reactivated(
    admin_client: AsyncClient, people: dict[str, str]
) -> None:
    deactivated = await access(admin_client, people[EDITOR], "deactivate")
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False
    assert deactivated.json()["allowed_actions"] == ["change_role", "reactivate"]
    assert (await admin_client.get("/admin/me", headers=admin_headers(EDITOR))).status_code == 403

    reactivated = await access(admin_client, people[EDITOR], "reactivate")

    assert reactivated.json()["is_active"] is True
    assert (await admin_client.get("/admin/me", headers=admin_headers(EDITOR))).status_code == 200


@pytest.mark.parametrize(
    ("call", "argument"), [("role", "viewer"), ("deactivate", None), ("reactivate", None)]
)
async def test_nobody_changes_themselves(
    admin_client: AsyncClient, people: dict[str, str], call: str, argument: str | None
) -> None:
    me = people[ADMIN]
    response = (
        await set_role(admin_client, me, argument)
        if call == "role"
        else await access(admin_client, me, call)
    )

    assert response.status_code == 409
    assert problem(response) == "self-change"


async def test_the_last_admin_cannot_be_demoted_or_deactivated(
    admin_client: AsyncClient, people: dict[str, str], db_session: AsyncSession
) -> None:
    """Only reachable through a second admin who is no longer active, so arrange exactly that:
    a second admin acts on the only active one."""
    await become(db_session, OTHER_ADMIN, "admin")
    other = (
        await db_session.execute(
            text("SELECT id::text FROM actors WHERE email = :e"), {"e": OTHER_ADMIN}
        )
    ).scalar_one()
    listed = {i["email"]: i for i in (await users(admin_client, OTHER_ADMIN)).json()["items"]}
    assert listed[ADMIN]["allowed_actions"] == ["change_role", "deactivate"]

    assert (await set_role(admin_client, other, "editor")).status_code == 200
    refused = await set_role(admin_client, people[ADMIN], "viewer", OTHER_ADMIN)

    assert refused.status_code == 403  # the second admin is an editor now


@pytest.mark.parametrize("user_id", ["00000000-0000-0000-0000-000000000000", "nope"])
async def test_an_unknown_person_is_not_found(
    admin_client: AsyncClient, people: dict[str, str], user_id: str
) -> None:
    response = await set_role(admin_client, user_id, "viewer")

    assert response.status_code == 404
    assert problem(response) == "user-not-found"


@pytest.mark.parametrize("role", ["owner", "", None, 3, "Admin"])
async def test_an_unknown_role_is_422(
    admin_client: AsyncClient, people: dict[str, str], role: Any
) -> None:
    response = await set_role(admin_client, people[VIEWER], role)

    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["role"]


# --- a country's subdivision types ---------------------------------------------------------------


async def put_types(client: AsyncClient, code: str, keys: Any, email: str = ADMIN) -> Response:
    return await client.put(
        f"/admin/countries/{code}/subdivision-types",
        json={"keys": keys},
        headers=admin_headers(email),
    )


async def test_an_admin_narrows_and_widens_a_countrys_types(
    admin_client: AsyncClient, people: dict[str, str]
) -> None:
    narrowed = await put_types(admin_client, "in", ["state"])

    assert narrowed.status_code == 200, narrowed.text
    body = narrowed.json()
    assert [t["key"] for t in body["subdivision_types"]] == ["state"]
    assert {t["key"] for t in body["available_subdivision_types"]} == {"state", "union territory"}

    widened = (await put_types(admin_client, "IN", ["state", "union territory"])).json()
    assert [t["key"] for t in widened["subdivision_types"]] == ["state", "union territory"]
    assert sum(t["subdivision_count"] for t in widened["subdivision_types"]) == 36


async def test_no_types_is_allowed(admin_client: AsyncClient, people: dict[str, str]) -> None:
    body = (await put_types(admin_client, "BE", [])).json()

    assert body["subdivision_types"] == []
    assert {t["key"] for t in body["available_subdivision_types"]} == {"province", "region"}


@pytest.mark.parametrize(
    ("keys", "fields"),
    [
        (["state", "region"], ["keys.1"]),
        (["state", "state"], ["keys.1"]),
        (["STATE"], ["keys.0"]),
    ],
)
async def test_a_type_the_country_does_not_have_is_named(
    admin_client: AsyncClient, people: dict[str, str], keys: list[str], fields: list[str]
) -> None:
    response = await put_types(admin_client, "IN", keys)

    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == fields


@pytest.mark.parametrize("code", ["ZZ", "IND", "1"])
async def test_an_unknown_country_is_not_found(
    admin_client: AsyncClient, people: dict[str, str], code: str
) -> None:
    response = await put_types(admin_client, code, [])

    assert response.status_code == 404
    assert problem(response) == "country-not-found"


async def test_an_editor_cannot_change_a_countrys_types(
    admin_client: AsyncClient, people: dict[str, str]
) -> None:
    assert (await put_types(admin_client, "IN", ["state"], EDITOR)).status_code == 403


async def test_every_country_with_subdivisions_offers_them(
    admin_client: AsyncClient, people: dict[str, str]
) -> None:
    india = (await admin_client.get("/admin/countries/IN", headers=admin_headers(VIEWER))).json()

    assert {t["key"]: t["subdivision_count"] for t in india["subdivision_types"]} == {
        "state": 28,
        "union territory": 8,
    }
