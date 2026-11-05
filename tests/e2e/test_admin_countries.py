"""`/admin/countries`: the countries page and one country's page.

Counts are checked against `/admin/catalogs`, whose default (published) the countries page links to:
the two must never disagree.
"""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from registry.domain.enums import CatalogKind
from registry.schemas.admin import KindCounts
from tests.conftest import admin_headers
from tests.e2e.test_admin_api import PROBLEMS, become, list_catalogs

pytestmark = pytest.mark.e2e


async def list_countries(client: AsyncClient, **params: Any) -> Any:
    response = await client.get("/admin/countries", params=params, headers=admin_headers())
    assert response.status_code == 200, response.text
    return response.json()


async def read_country(client: AsyncClient, code: str) -> Any:
    response = await client.get(f"/admin/countries/{code}", headers=admin_headers())
    assert response.status_code == 200, response.text
    return response.json()


async def test_only_countries_with_a_catalog_are_listed_most_published_first(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    used = set(
        (
            await db_session.execute(
                text("SELECT DISTINCT country_code FROM catalogs WHERE country_code IS NOT NULL")
            )
        ).scalars()
    )

    body = await list_countries(admin_client)
    items = body["items"]

    assert {item["code"] for item in items} == used
    assert body["total"] == len(used)
    assert "IS" not in used
    order = [(-item["published_catalog_count"], item["name"], item["code"]) for item in items]
    assert order == sorted(order)


async def test_a_country_whose_catalogs_are_all_drafts_is_listed_with_zero(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await db_session.execute(text("UPDATE catalogs SET status = 'draft' WHERE country_code = 'BE'"))
    await db_session.commit()

    items = (await list_countries(admin_client))["items"]

    assert {"code": "BE", "name": "Belgium", "published_catalog_count": 0} in items


async def test_no_catalogs_lists_no_countries_but_each_still_has_its_page(
    admin_client: AsyncClient,
) -> None:
    body = await list_countries(admin_client, page=50)

    assert body["items"] == []
    assert body["total"] == 0
    assert (await read_country(admin_client, "IS"))["code"] == "IS"


async def test_each_count_is_what_the_catalog_list_shows_for_that_country(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    busiest = (
        await db_session.execute(
            text(
                "SELECT country_code FROM catalogs WHERE country_code IS NOT NULL "
                "GROUP BY country_code ORDER BY count(*) DESC, country_code LIMIT 1"
            )
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "UPDATE catalogs SET status = 'draft' WHERE id = "
            "(SELECT id FROM catalogs WHERE country_code = :c ORDER BY id LIMIT 1)"
        ),
        {"c": busiest},
    )
    await db_session.commit()

    items = (await list_countries(admin_client))["items"]
    used = [item for item in items if item["published_catalog_count"] > 0]

    assert any(item["code"] == busiest for item in used)
    for item in used:
        shown = await list_catalogs(admin_client, country=item["code"])
        assert item["published_catalog_count"] == shown["total"], item["code"]


@pytest.mark.parametrize("q", ["Belgium", "belgique", "BELGIË", "belgie", "be", "BE", "  be  "])
async def test_a_name_in_any_language_without_accents_or_the_code_finds_a_country(
    admin_client: AsyncClient, searchable_catalogs: None, q: str
) -> None:
    codes = [item["code"] for item in (await list_countries(admin_client, q=q))["items"]]

    assert "BE" in codes


async def test_a_search_that_matches_nothing_is_an_empty_page(admin_client: AsyncClient) -> None:
    body = await list_countries(admin_client, q="zzzz")

    assert body == {"items": [], "total": 0, "page": 1, "per_page": 100}


async def test_a_page_past_the_end_keeps_the_total(
    admin_client: AsyncClient, searchable_catalogs: None
) -> None:
    body = await list_countries(admin_client, page=50)

    assert body["items"] == []
    assert body["total"] > 0


@pytest.mark.parametrize("bad", [{"page": 0}, {"per_page": 0}, {"per_page": 101}])
async def test_invalid_paging_is_a_422_naming_the_field(
    admin_client: AsyncClient, bad: dict[str, Any]
) -> None:
    response = await admin_client.get("/admin/countries", params=bad, headers=admin_headers())

    assert response.status_code == 422
    assert response.json()["type"] == f"{PROBLEMS}validation"
    assert [e["field"] for e in response.json()["errors"]] == list(bad)


async def test_a_country_shows_its_languages_and_subdivision_types(
    admin_client: AsyncClient,
) -> None:
    belgium = await read_country(admin_client, "be")

    assert belgium["code"] == "BE"
    assert belgium["name"] == "Belgium"
    assert belgium["official_languages"] == [
        {"tag": "nl", "name": "Dutch"},
        {"tag": "fr", "name": "French"},
        {"tag": "de", "name": "German"},
    ]
    assert belgium["subdivision_types"] == [
        {"key": "province", "label": "Province", "subdivision_count": 0, "subdivisions": []},
        {
            "key": "region",
            "label": "Region",
            "subdivision_count": 3,
            "subdivisions": [
                {"code": "BE-BRU", "name": "Brussels"},
                {"code": "BE-VLG", "name": "Flanders"},
                {"code": "BE-WAL", "name": "Wallonia"},
            ],
        },
    ]
    assert belgium["available_subdivision_types"] == [
        {"key": "province", "label": "Province"},
        {"key": "region", "label": "Region"},
    ]


async def test_a_country_with_no_catalogs_and_no_types_is_all_zeros(
    admin_client: AsyncClient,
) -> None:
    body = await read_country(admin_client, "IS")

    assert body["subdivision_types"] == body["available_subdivision_types"] == []
    assert body["kpis"] == {
        "by_status": {"draft": 0, "published": 0, "unpublished": 0, "archived": 0},
        "recommended": 0,
        "by_kind": {kind.value: 0 for kind in CatalogKind},
    }


async def test_a_country_kpis_agree_with_the_catalog_list(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await db_session.execute(
        text(
            "UPDATE catalogs SET status = 'archived' WHERE id = "
            "(SELECT id FROM catalogs WHERE country_code = 'FR' ORDER BY id LIMIT 1)"
        )
    )
    await db_session.commit()

    kpis = (await read_country(admin_client, "FR"))["kpis"]

    for status, count in kpis["by_status"].items():
        assert count == (await list_catalogs(admin_client, country="FR", status=status))["total"]
    assert kpis["by_status"]["archived"] == 1
    recommended = await list_catalogs(admin_client, country="FR", recommended="true")
    assert kpis["recommended"] == recommended["total"]
    for kind, count in kpis["by_kind"].items():
        assert count == (await list_catalogs(admin_client, country="FR", kind=kind))["total"]


@pytest.mark.parametrize("code", ["ZZ", "zz", "FRA", "F", "1A", "%20%20"])
async def test_an_unknown_or_malformed_code_is_country_not_found(
    admin_client: AsyncClient, code: str
) -> None:
    response = await admin_client.get(f"/admin/countries/{code}", headers=admin_headers())

    assert response.status_code == 404
    assert response.json()["type"] == f"{PROBLEMS}country-not-found"


@pytest.mark.parametrize("path", ["/admin/countries", "/admin/countries/FR"])
async def test_countries_need_credentials_and_any_role_reads_them(
    admin_client: AsyncClient, db_session: AsyncSession, path: str
) -> None:
    await become(db_session, "countries-viewer@edrlab.org", "viewer")

    refused = await admin_client.get(path)
    read = await admin_client.get(path, headers=admin_headers("countries-viewer@edrlab.org"))

    assert refused.status_code == 401
    assert read.status_code == 200
    assert read.headers["cache-control"] == "no-store"


async def test_each_country_read_is_one_statement(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    db_connection: AsyncConnection,
    searchable_catalogs: None,
) -> None:
    """R4. The login is one more statement."""
    await become(db_session, "count@edrlab.org", "viewer")
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if "SAVEPOINT" not in statement:
            seen.append(statement)

    event.listen(db_connection.sync_connection, "before_cursor_execute", record)
    try:
        counts = []
        for path in ("/admin/countries", "/admin/countries/FR"):
            seen.clear()
            await admin_client.get(path, headers=admin_headers("count@edrlab.org"))
            counts.append(len(seen))
    finally:
        event.remove(db_connection.sync_connection, "before_cursor_execute", record)

    assert counts == [2, 2]


def test_kind_counts_have_one_field_per_catalog_kind() -> None:
    assert list(KindCounts.model_fields) == [kind.value for kind in CatalogKind]


async def test_the_openapi_document_types_the_kind_counts(admin_client: AsyncClient) -> None:
    document = (await admin_client.get("/openapi.json")).json()

    assert {"/admin/countries", "/admin/countries/{code}"} <= set(document["paths"])
    assert set(document["components"]["schemas"]["KindCounts"]["required"]) == {
        kind.value for kind in CatalogKind
    }
