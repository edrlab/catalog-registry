"""Your own profile: `GET /admin/me` in full, and `PATCH /admin/me {name}`."""

from typing import Any

import pytest
from httpx import AsyncClient, Response

from tests.conftest import admin_headers

pytestmark = pytest.mark.e2e

ME = "profile.person@edrlab.org"


async def me(client: AsyncClient, email: str = ME) -> Any:
    response = await client.get("/admin/me", headers=admin_headers(email))
    assert response.status_code == 200, response.text
    return response.json()


async def rename(client: AsyncClient, body: Any, email: str = ME) -> Response:
    return await client.patch("/admin/me", json=body, headers=admin_headers(email))


async def test_me_shows_the_whole_account(admin_client: AsyncClient) -> None:
    first = await me(admin_client)
    second = await me(admin_client)

    assert first["created_at"] is not None
    assert first["is_active"] is True
    assert first["last_seen_at"] is None
    assert second["last_seen_at"] is not None  # the first visit
    assert second["created_at"] == first["created_at"]
    assert "identity_issuer" in second
    assert "identity_subject" in second


async def test_anyone_sets_and_clears_their_name(admin_client: AsyncClient) -> None:
    await me(admin_client)

    named = await rename(admin_client, {"name": "  Ada Lovelace  "})

    assert named.status_code == 200, named.text
    assert named.json()["name"] == "Ada Lovelace"
    assert named.json()["name_is_set"] is True
    assert (await me(admin_client))["name"] == "Ada Lovelace"  # a later visit keeps it

    for cleared in (None, "", "   "):
        response = await rename(admin_client, {"name": cleared})
        assert response.status_code == 200
        assert response.json()["name"] == "profile.person"
        assert response.json()["name_is_set"] is False


@pytest.mark.parametrize("name", ["x" * 101, "tab\there", "nul\x00", 42])
async def test_a_bad_name_is_422(admin_client: AsyncClient, name: Any) -> None:
    response = await rename(admin_client, {"name": name})

    assert response.status_code == 422
    assert [error["field"] for error in response.json()["errors"]] == ["name"]


@pytest.mark.parametrize("body", [{"role": "admin"}, {"name": "x", "is_active": False}, {}])
async def test_only_the_name_is_self_editable(admin_client: AsyncClient, body: Any) -> None:
    response = await rename(admin_client, body)

    assert response.status_code == 422
    assert (await me(admin_client))["role"] == "viewer"
