"""`/admin/languages`: every language the registry can name, for the catalog form's picker."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry.services.language_names import build_language_name
from tests.conftest import admin_headers
from tests.e2e.test_admin_api import become

pytestmark = pytest.mark.e2e


async def list_languages(client: AsyncClient) -> list[dict[str, Any]]:
    response = await client.get("/admin/languages", headers=admin_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == ["items"]
    items: list[dict[str, Any]] = body["items"]
    return items


async def test_each_language_is_a_tag_and_its_one_name(admin_client: AsyncClient) -> None:
    items = await list_languages(admin_client)

    assert 600 < len(items) < 800
    assert all(list(item) == ["tag", "name"] for item in items)
    assert all(item["tag"] == item["tag"].lower() and len(item["tag"]) in (2, 3) for item in items)
    # The same name the country page and the catalog give the tag, never a second one.
    assert all(item["name"] == build_language_name(item["tag"]) for item in items)
    assert len({item["tag"] for item in items}) == len(items)


async def test_three_letter_languages_are_listed(admin_client: AsyncClient) -> None:
    names = {item["tag"]: item["name"] for item in await list_languages(admin_client)}

    assert names["kha"] == "Khasi"
    assert names["brx"] == "Bodo"
    assert names["sat"] == "Santali"
    assert names["cak"] == "Kaqchikel"  # named by ISO 639-3, not CLDR
    assert names["fr"] == "French"


async def test_special_codes_aliases_and_regional_variants_are_left_out(
    admin_client: AsyncClient,
) -> None:
    tags = {item["tag"] for item in await list_languages(admin_client)}

    assert not tags & {"und", "mul", "zxx", "mis", "root"}
    assert not tags & {"iw", "in", "ji", "mo", "tl", "sh"}
    assert {"he", "id", "yi", "ro", "fil", "sr"} <= tags
    assert not any("-" in tag or "_" in tag for tag in tags)


async def test_languages_are_sorted_by_name(admin_client: AsyncClient) -> None:
    names = [item["name"] for item in await list_languages(admin_client)]

    assert names == sorted(names, key=str.casefold)


async def test_languages_need_credentials_and_any_role_reads_them(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    await become(db_session, "languages-viewer@edrlab.org", "viewer")

    refused = await admin_client.get("/admin/languages")
    read = await admin_client.get(
        "/admin/languages", headers=admin_headers("languages-viewer@edrlab.org")
    )

    assert refused.status_code == 401
    assert read.status_code == 200
    assert read.headers["cache-control"] == "no-store"
