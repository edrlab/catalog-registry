"""Icons stored by the image Worker (faked here): `PUT /admin/catalogs/{id}/icon` with an image,
an icon link added by a save, and `POST /admin/catalogs/{id}/icon/store` to try again. Each time the
catalog's icon links become one link to the stored PNG."""

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry.core.errors import AdminUnavailableError, FieldValidationError
from registry.domain.fetching import FetchError
from registry.network.icon_worker import ICON_IS_SVG
from tests.conftest import admin_headers
from tests.e2e.test_admin_api import become
from tests.e2e.test_admin_catalog_edits import create, edit, form_body, new_catalog
from tests.e2e.test_admin_catalog_writes import problem_type, status_and_version
from tests.search_helpers import assert_valid_catalog

pytestmark = pytest.mark.e2e

EDITOR = "editor@edrlab.org"
PNG = b"\x89PNG\r\n\x1a\n fake bytes; the fake Worker does not decode them"
STORED = "https://icons.example.org/{id}-icon.png?v=abc123"


@pytest.fixture
async def stored(admin_app: FastAPI) -> list[tuple[str, bytes, str]]:
    """What reached the fake Worker: catalog id, bytes, media type."""
    calls: list[tuple[str, bytes, str]] = []

    async def fake_worker(catalog_id: str, image: bytes, media_type: str) -> str:
        calls.append((catalog_id, image, media_type))
        return STORED.format(id=catalog_id)

    admin_app.state.persist_catalog_icon = fake_worker
    return calls


@pytest.fixture
async def catalog(admin_client: AsyncClient, searchable_catalogs: None) -> dict[str, Any]:
    """A published catalog that has an icon link, as its detail page reads it."""
    listing = await admin_client.get("/admin/catalogs", headers=admin_headers())
    for item in listing.json()["items"]:
        if item["icon_url"]:
            detail = await admin_client.get(
                f"/admin/catalogs/{item['id']}", headers=admin_headers()
            )
            body: dict[str, Any] = detail.json()
            return body
    raise AssertionError("the seed has no catalog with an icon")


async def upload(
    client: AsyncClient,
    catalog_id: str,
    version: int,
    *,
    email: str = EDITOR,
    body: bytes = PNG,
    media_type: str = "image/png",
) -> Any:
    return await client.put(
        f"/admin/catalogs/{catalog_id}/icon",
        params={"version": version},
        content=body,
        headers=admin_headers(email) | {"Content-Type": media_type},
    )


async def test_an_upload_replaces_the_icon_links_with_the_stored_png(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    stored: list[tuple[str, bytes, str]],
) -> None:
    await become(db_session, EDITOR, "editor")
    others = [link for link in catalog["links"] if link["rel"] not in ("icon", "self")]

    response = await upload(
        admin_client, catalog["id"], catalog["version"], media_type="image/jpeg"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    icons = [link for link in body["links"] if link["rel"] == "icon"]
    assert [(link["href"], link["type"]) for link in icons] == [
        (STORED.format(id=catalog["id"]), "image/png")
    ]
    assert [link for link in body["links"] if link["rel"] not in ("icon", "self")] == others
    assert body["icon_url"] == STORED.format(id=catalog["id"])
    assert body["version"] == catalog["version"] + 1
    assert stored == [(catalog["id"], PNG, "image/jpeg")]
    public = (await admin_client.get(f"/catalogs/{catalog['id']}")).json()
    assert_valid_catalog(public)
    assert [link["href"] for link in public["links"] if link["rel"] == "icon"] == [
        STORED.format(id=catalog["id"])
    ]


async def test_a_stale_version_is_refused_before_anything_is_stored(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    stored: list[tuple[str, bytes, str]],
) -> None:
    await become(db_session, EDITOR, "editor")

    response = await upload(admin_client, catalog["id"], catalog["version"] + 1)

    assert (response.status_code, problem_type(response)) == (409, "version-conflict")
    assert stored == []
    assert await status_and_version(db_session, catalog["id"]) == ("published", catalog["version"])


async def test_a_viewer_cannot_upload(
    admin_client: AsyncClient, catalog: dict[str, Any], stored: list[tuple[str, bytes, str]]
) -> None:
    response = await upload(admin_client, catalog["id"], catalog["version"], email="v@edrlab.org")

    assert response.status_code == 403
    assert stored == []


@pytest.mark.parametrize(
    "sent",
    [(PNG, "application/json"), (b"", "image/png")],
    ids=["not-an-image-type", "empty"],
)
async def test_a_body_that_is_not_an_icon_is_refused_before_the_worker(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    stored: list[tuple[str, bytes, str]],
    sent: tuple[bytes, str],
) -> None:
    await become(db_session, EDITOR, "editor")
    body, media_type = sent

    response = await upload(
        admin_client, catalog["id"], catalog["version"], body=body, media_type=media_type
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "body"
    assert stored == []


async def test_without_a_worker_configured_uploads_are_unavailable(
    admin_client: AsyncClient, db_session: AsyncSession, catalog: dict[str, Any]
) -> None:
    await become(db_session, EDITOR, "editor")

    response = await upload(admin_client, catalog["id"], catalog["version"])

    assert (response.status_code, problem_type(response)) == (503, "unavailable")
    assert await status_and_version(db_session, catalog["id"]) == ("published", catalog["version"])


async def test_an_image_over_5_mb_has_its_own_problem_type(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    stored: list[tuple[str, bytes, str]],
) -> None:
    await become(db_session, EDITOR, "editor")

    response = await upload(
        admin_client, catalog["id"], catalog["version"], body=b"\0" * (5 * 1024 * 1024 + 1)
    )

    assert (response.status_code, problem_type(response)) == (413, "icon-too-large")
    assert stored == []


async def test_a_catalog_without_an_icon_gets_one(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    searchable_catalogs: None,
    stored: list[tuple[str, bytes, str]],
) -> None:
    await become(db_session, EDITOR, "editor")
    listing = (await admin_client.get("/admin/catalogs", headers=admin_headers())).json()
    bare = next(item for item in listing["items"] if item["icon_url"] is None)

    response = await upload(admin_client, bare["id"], bare["version"])

    assert response.status_code == 200, response.text
    assert response.json()["icon_url"] == STORED.format(id=bare["id"])
    public = (await admin_client.get(f"/catalogs/{bare['id']}")).json()
    assert_valid_catalog(public)
    assert [link["href"] for link in public["links"] if link["rel"] == "icon"] == [
        STORED.format(id=bare["id"])
    ]


LIBRARY_ICON = "https://library.example.org/apple-touch-icon.png"
SITE_DOWN = FetchError("fetch-failed", "The site answered 503.", url=LIBRARY_ICON)
SVG_PROBLEM = (
    "We don't support this icon's format (SVG) yet, so it can't be saved on our servers. "
    "The library's own icon is used."
)
COULD_NOT_FETCH = (
    "We couldn't fetch the icon from the library's website just now, so its own icon is "
    "used for now. Try again later."
)


@dataclass
class Downloads:
    """The icons the registry downloaded, and what a download raises instead, by href."""

    hrefs: list[str] = field(default_factory=list)
    failures: dict[str, Exception] = field(default_factory=dict)


@pytest.fixture
async def downloads(admin_app: FastAPI, stored: list[tuple[str, bytes, str]]) -> Downloads:
    """Icon storage configured, with downloads and the Worker faked."""
    admin_app.state.settings = admin_app.state.settings.model_copy(
        update={"icon_base_url": "https://icons.example.org"}
    )
    seen = Downloads()

    async def fake_fetch(href: str) -> tuple[bytes, str]:
        seen.hrefs.append(href)
        if href in seen.failures:
            raise seen.failures[href]
        return PNG, "image/png"

    admin_app.state.fetch_catalog_icon = fake_fetch
    return seen


def with_icon(body: dict[str, Any], href: str) -> dict[str, Any]:
    links = [link for link in body["links"] if link["rel"] != "icon"]
    return body | {"links": [*links, {"rel": "icon", "href": href, "type": "image/png"}]}


def icon_hrefs(detail: dict[str, Any]) -> list[str]:
    return [link["href"] for link in detail["links"] if link["rel"] == "icon"]


async def test_an_edit_that_adds_an_icon_link_stores_the_icon(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    stored: list[tuple[str, bytes, str]],
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")

    response = await edit(admin_client, catalog["id"], with_icon(form_body(catalog), LIBRARY_ICON))

    assert response.status_code == 200, response.text
    body = response.json()
    assert downloads.hrefs == [LIBRARY_ICON]
    assert stored == [(catalog["id"], PNG, "image/png")]
    assert icon_hrefs(body) == [STORED.format(id=catalog["id"])]
    assert (body["icon_stored"], body["icon_problem"]) == (True, None)
    # The edit, then the icon: the form carries this version into its next save.
    assert body["version"] == catalog["version"] + 2
    assert_valid_catalog((await admin_client.get(f"/catalogs/{catalog['id']}")).json())


async def test_an_edit_that_keeps_the_icon_downloads_nothing(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    stored: list[tuple[str, bytes, str]],
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")

    response = await edit(admin_client, catalog["id"], form_body(catalog) | {"name": "Renamed"})

    assert response.status_code == 200, response.text
    assert (downloads.hrefs, stored) == ([], [])
    assert icon_hrefs(response.json()) == icon_hrefs(catalog)
    assert response.json()["icon_stored"] is False


async def test_when_the_icon_cannot_be_downloaded_the_save_keeps_its_link_and_says_why(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    stored: list[tuple[str, bytes, str]],
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")
    downloads.failures[LIBRARY_ICON] = SITE_DOWN

    response = await edit(admin_client, catalog["id"], with_icon(form_body(catalog), LIBRARY_ICON))

    assert response.status_code == 200, response.text
    body = response.json()
    assert icon_hrefs(body) == [LIBRARY_ICON]
    assert body["icon_stored"] is False
    assert body["icon_problem"] == COULD_NOT_FETCH
    assert body["version"] == catalog["version"] + 1
    assert stored == []


async def test_an_icon_the_worker_cannot_convert_keeps_its_link_and_says_why(
    admin_app: FastAPI,
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")

    async def refusing_worker(catalog_id: str, image: bytes, media_type: str) -> str:
        raise FieldValidationError([{"field": "body", "message": ICON_IS_SVG}])

    admin_app.state.persist_catalog_icon = refusing_worker

    response = await edit(admin_client, catalog["id"], with_icon(form_body(catalog), LIBRARY_ICON))

    assert response.status_code == 200, response.text
    body = response.json()
    assert icon_hrefs(body) == [LIBRARY_ICON]
    assert body["icon_problem"] == SVG_PROBLEM


async def test_a_new_catalog_with_an_icon_link_has_its_icon_stored(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    searchable_catalogs: None,
    stored: list[tuple[str, bytes, str]],
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")
    body = new_catalog()

    response = await create(admin_client, with_icon(body, LIBRARY_ICON))

    assert response.status_code == 201, response.text
    created = response.json()
    assert downloads.hrefs == [LIBRARY_ICON]
    assert icon_hrefs(created) == [STORED.format(id=created["id"])]
    assert created["icon_stored"] is True


async def store_again(client: AsyncClient, catalog_id: str, version: int) -> Any:
    return await client.post(
        f"/admin/catalogs/{catalog_id}/icon/store",
        params={"version": version},
        headers=admin_headers(EDITOR),
    )


async def test_trying_again_stores_the_icon_link(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    stored: list[tuple[str, bytes, str]],
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")

    response = await store_again(admin_client, catalog["id"], catalog["version"])

    assert response.status_code == 200, response.text
    assert downloads.hrefs[0] in icon_hrefs(catalog)
    assert icon_hrefs(response.json()) == [STORED.format(id=catalog["id"])]
    assert response.json()["icon_stored"] is True
    assert response.json()["version"] == catalog["version"] + 1


async def test_trying_again_while_the_site_is_down_says_why_and_changes_nothing(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    stored: list[tuple[str, bytes, str]],
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")
    for href in icon_hrefs(catalog):
        downloads.failures[href] = SITE_DOWN

    response = await store_again(admin_client, catalog["id"], catalog["version"])

    assert response.status_code == 422
    error = response.json()["errors"][0]
    assert error["field"] == "icon"
    assert error["message"] == COULD_NOT_FETCH
    assert stored == []
    assert await status_and_version(db_session, catalog["id"]) == ("published", catalog["version"])


async def test_trying_again_with_a_stale_version_downloads_nothing(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")

    response = await store_again(admin_client, catalog["id"], catalog["version"] + 1)

    assert (response.status_code, problem_type(response)) == (409, "version-conflict")
    assert downloads.hrefs == []


async def test_trying_again_without_an_icon_link_is_refused(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    searchable_catalogs: None,
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")
    listing = (await admin_client.get("/admin/catalogs", headers=admin_headers())).json()
    bare = next(item for item in listing["items"] if item["icon_url"] is None)

    response = await store_again(admin_client, bare["id"], bare["version"])

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "icon"
    assert downloads.hrefs == []


async def test_trying_again_without_icon_storage_configured_is_unavailable(
    admin_client: AsyncClient, db_session: AsyncSession, catalog: dict[str, Any]
) -> None:
    await become(db_session, EDITOR, "editor")

    response = await store_again(admin_client, catalog["id"], catalog["version"])

    assert (response.status_code, problem_type(response)) == (503, "unavailable")


async def test_trying_again_with_a_file_the_worker_refuses_names_the_icon_field(
    admin_app: FastAPI,
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")

    async def refusing_worker(catalog_id: str, image: bytes, media_type: str) -> str:
        raise FieldValidationError([{"field": "body", "message": ICON_IS_SVG}])

    admin_app.state.persist_catalog_icon = refusing_worker

    response = await store_again(admin_client, catalog["id"], catalog["version"])

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {
            "field": "icon",
            "message": SVG_PROBLEM,
        }
    ]


@pytest.fixture
async def removed(admin_app: FastAPI) -> list[str]:
    """The catalog ids whose stored icon the registry asked the Worker to delete."""
    calls: list[str] = []

    async def fake_remove(catalog_id: str) -> None:
        calls.append(catalog_id)

    admin_app.state.remove_catalog_icon = fake_remove
    return calls


async def stored_icon_catalog(client: AsyncClient, catalog: dict[str, Any]) -> dict[str, Any]:
    """*catalog* after its icon was uploaded: its one icon link is the stored PNG."""
    response = await upload(client, catalog["id"], catalog["version"])
    assert response.status_code == 200, response.text
    detail: dict[str, Any] = response.json()
    assert detail["icon_stored"] is True
    return detail


def without_icon(body: dict[str, Any]) -> dict[str, Any]:
    return body | {"links": [link for link in body["links"] if link["rel"] != "icon"]}


async def test_a_save_that_keeps_the_stored_icon_drops_any_other_icon_link(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    downloads: Downloads,
    removed: list[str],
) -> None:
    await become(db_session, EDITOR, "editor")
    stored = await stored_icon_catalog(admin_client, catalog)
    body = form_body(stored)
    body["links"].append({"rel": "icon", "href": LIBRARY_ICON, "type": "image/png"})

    response = await edit(admin_client, stored["id"], body)

    assert response.status_code == 200, response.text
    assert icon_hrefs(response.json()) == icon_hrefs(stored)
    assert response.json()["version"] == stored["version"]
    assert (downloads.hrefs, removed) == ([], [])


async def test_replacing_the_stored_icon_with_another_stores_that_one(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    downloads: Downloads,
    removed: list[str],
) -> None:
    await become(db_session, EDITOR, "editor")
    current = await stored_icon_catalog(admin_client, catalog)

    response = await edit(admin_client, current["id"], with_icon(form_body(current), LIBRARY_ICON))

    assert response.status_code == 200, response.text
    assert downloads.hrefs == [LIBRARY_ICON]
    assert response.json()["icon_stored"] is True
    assert removed == []


async def test_a_replacement_that_cannot_be_stored_deletes_the_old_file(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    downloads: Downloads,
    removed: list[str],
) -> None:
    await become(db_session, EDITOR, "editor")
    current = await stored_icon_catalog(admin_client, catalog)
    downloads.failures[LIBRARY_ICON] = SITE_DOWN

    response = await edit(admin_client, current["id"], with_icon(form_body(current), LIBRARY_ICON))

    assert response.status_code == 200, response.text
    assert icon_hrefs(response.json()) == [LIBRARY_ICON]
    assert response.json()["icon_problem"] == COULD_NOT_FETCH
    assert removed == [current["id"]]


async def test_removing_the_stored_icon_deletes_its_file(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    downloads: Downloads,
    removed: list[str],
) -> None:
    await become(db_session, EDITOR, "editor")
    current = await stored_icon_catalog(admin_client, catalog)

    response = await edit(admin_client, current["id"], without_icon(form_body(current)))

    assert response.status_code == 200, response.text
    assert response.json()["icon_url"] is None
    assert removed == [current["id"]]


async def test_an_edit_that_keeps_the_stored_icon_deletes_nothing(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    catalog: dict[str, Any],
    downloads: Downloads,
    removed: list[str],
) -> None:
    await become(db_session, EDITOR, "editor")
    current = await stored_icon_catalog(admin_client, catalog)

    response = await edit(admin_client, current["id"], form_body(current) | {"name": "Renamed"})

    assert response.status_code == 200, response.text
    assert removed == []


async def delete_draft(client: AsyncClient, detail: dict[str, Any]) -> Any:
    return await client.delete(
        f"/admin/catalogs/{detail['id']}",
        params={"version": detail["version"]},
        headers=admin_headers(EDITOR),
    )


async def test_deleting_a_draft_deletes_its_stored_icon(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    searchable_catalogs: None,
    downloads: Downloads,
    removed: list[str],
) -> None:
    await become(db_session, EDITOR, "editor")
    draft = (await create(admin_client, with_icon(new_catalog(), LIBRARY_ICON))).json()
    assert draft["icon_stored"] is True

    response = await delete_draft(admin_client, draft)

    assert response.status_code == 204
    assert removed == [draft["id"]]


async def test_deleting_a_draft_with_a_link_icon_deletes_nothing(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    searchable_catalogs: None,
    downloads: Downloads,
    removed: list[str],
) -> None:
    await become(db_session, EDITOR, "editor")
    downloads.failures[LIBRARY_ICON] = SITE_DOWN
    draft = (await create(admin_client, with_icon(new_catalog(), LIBRARY_ICON))).json()
    assert draft["icon_stored"] is False

    response = await delete_draft(admin_client, draft)

    assert response.status_code == 204
    assert removed == []


async def test_a_file_that_cannot_be_deleted_never_fails_the_change(
    admin_app: FastAPI,
    admin_client: AsyncClient,
    db_session: AsyncSession,
    searchable_catalogs: None,
    downloads: Downloads,
) -> None:
    await become(db_session, EDITOR, "editor")

    async def unreachable_worker(catalog_id: str) -> None:
        raise AdminUnavailableError("The image service answered 405.")

    admin_app.state.remove_catalog_icon = unreachable_worker
    draft = (await create(admin_client, with_icon(new_catalog(), LIBRARY_ICON))).json()

    response = await delete_draft(admin_client, draft)

    assert response.status_code == 204
    assert (await read_detail_status(admin_client, draft["id"])) == 404


async def read_detail_status(client: AsyncClient, catalog_id: str) -> int:
    return (await client.get(f"/admin/catalogs/{catalog_id}", headers=admin_headers())).status_code
