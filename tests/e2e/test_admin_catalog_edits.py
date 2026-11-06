"""`POST /admin/catalogs`, `PATCH /admin/catalogs/{id}` and `DELETE /admin/catalogs/{id}`.

Bodies are sent the way the back office's forms send them: every field, `null` for an empty
optional string, `[]` for an empty list, links with `type`/`title` only when filled.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import seed_catalogs
from tests.conftest import REPO_ROOT, admin_headers
from tests.e2e.test_admin_api import become
from tests.e2e.test_admin_catalog_writes import problem_type, status_and_version, transition

pytestmark = pytest.mark.e2e

EDITOR = "editor@edrlab.org"
VIEWER = "viewer@edrlab.org"
HREF = "https://example.org/opds/catalog.json"


@pytest.fixture
async def editor(db_session: AsyncSession, searchable_catalogs: None) -> str:
    await become(db_session, EDITOR, "editor")
    return EDITOR


def new_catalog(**changes: Any) -> dict[str, Any]:
    """A create body as the Add form sends it."""
    body: dict[str, Any] = {
        "name": "Bibliothèque de test",
        "description": None,
        "color": "blue",
        "kinds": ["public"],
        "coverage": "local",
        "country": "BE",
        "city": "Liège",
        "subdivisions": ["BE-WAL"],
        "languages": ["fr", "pt-BR"],
        "publication_types": ["ebook"],
        "links": [
            {"rel": "catalog", "href": HREF, "type": "application/opds+json"},
            {"rel": "search", "href": "https://example.org/search{?query}", "templated": True},
        ],
        "publish": False,
    }
    return body | changes


def form_body(detail: dict[str, Any]) -> dict[str, Any]:
    """The Edit form's PATCH body for a catalog read from `GET /admin/catalogs/{id}`."""
    country = detail["place"]["country"]
    return {
        "version": detail["version"],
        "name": detail["name"],
        "description": detail["description"],
        "color": detail["color"],
        "kinds": detail["kinds"],
        "coverage": detail["coverage"],
        "country": country["code"] if country else None,
        "city": detail["place"]["city"],
        "subdivisions": [s["code"] for s in detail["subdivisions"]],
        "languages": [language["tag"] for language in detail["languages"]],
        "publication_types": detail["publication_types"],
        "links": [
            {"rel": link["rel"], "href": link["href"]}
            | ({"type": link["type"]} if link["type"] else {})
            | ({"title": link["title"]} if link["title"] else {})
            | ({"templated": True} if link["templated"] else {})
            for link in detail["links"]
            if link["rel"] != "self"
        ],
    }


async def create(client: AsyncClient, body: Any, email: str = EDITOR) -> Response:
    return await client.post("/admin/catalogs", json=body, headers=admin_headers(email))


async def edit(client: AsyncClient, catalog_id: str, body: Any, email: str = EDITOR) -> Response:
    return await client.patch(
        f"/admin/catalogs/{catalog_id}", json=body, headers=admin_headers(email)
    )


async def delete(
    client: AsyncClient, catalog_id: str, version: Any, email: str = EDITOR
) -> Response:
    return await client.delete(
        f"/admin/catalogs/{catalog_id}", params={"version": version}, headers=admin_headers(email)
    )


async def read(client: AsyncClient, catalog_id: str) -> Response:
    return await client.get(f"/admin/catalogs/{catalog_id}", headers=admin_headers(EDITOR))


async def all_catalog_ids(db_session: AsyncSession) -> list[str]:
    rows = await db_session.execute(text("SELECT id::text FROM catalogs ORDER BY id"))
    return [row[0] for row in rows]


def error_fields(response: Response) -> list[str]:
    assert response.status_code == 422, response.text
    assert problem_type(response) == "validation"
    return [error["field"] for error in response.json()["errors"]]


# --- create --------------------------------------------------------------------------------------


async def test_a_new_catalog_is_a_draft_with_every_field_as_sent(
    admin_client: AsyncClient, editor: str
) -> None:
    response = await create(admin_client, new_catalog())

    assert response.status_code == 201, response.text
    catalog = response.json()
    assert catalog["id"] == str(uuid.uuid5(uuid.NAMESPACE_URL, HREF))
    assert catalog["status"] == "draft"
    assert catalog["version"] == 1
    assert catalog["recommended"] is False
    assert catalog["public_url"] is None
    assert catalog["published_at"] is None
    assert catalog["allowed_actions"] == ["edit", "publish", "delete"]
    assert catalog["name"] == "Bibliothèque de test"
    assert catalog["color"] == "blue"
    assert catalog["kinds"] == ["public"]
    assert catalog["coverage"] == "local"
    assert catalog["place"]["city"] == "Liège"
    assert catalog["place"]["country"]["code"] == "BE"
    assert [s["code"] for s in catalog["subdivisions"]] == ["BE-WAL"]
    assert sorted(language["tag"] for language in catalog["languages"]) == ["fr", "pt-br"]
    assert catalog["publication_types"] == ["ebook"]
    links = {link["rel"]: link for link in catalog["links"]}
    assert links["catalog"]["href"] == HREF
    assert links["catalog"]["type"] == "application/opds+json"
    assert links["search"]["templated"] is True
    assert (await read(admin_client, catalog["id"])).json() == catalog


async def test_a_catalog_created_published_is_served_to_readers(
    admin_client: AsyncClient, editor: str
) -> None:
    response = await create(admin_client, new_catalog(publish=True))

    assert response.status_code == 201, response.text
    catalog = response.json()
    assert catalog["status"] == "published"
    assert catalog["published_at"] is not None
    assert catalog["public_url"].endswith(f"/catalogs/{catalog['id']}")
    public = await admin_client.get(f"/catalogs/{catalog['id']}")
    assert public.status_code == 200
    assert public.json()["metadata"]["title"] == "Bibliothèque de test"


async def test_a_draft_is_not_served_to_readers(admin_client: AsyncClient, editor: str) -> None:
    catalog = (await create(admin_client, new_catalog())).json()

    assert (await admin_client.get(f"/catalogs/{catalog['id']}")).status_code == 404


async def test_optional_fields_may_be_left_out(admin_client: AsyncClient, editor: str) -> None:
    body = {"name": "Bare", "kinds": ["open"], "links": [{"rel": "shelf", "href": HREF}]}

    response = await create(admin_client, body)

    assert response.status_code == 201, response.text
    catalog = response.json()
    assert catalog["color"] == "gray"
    assert catalog["coverage"] is None
    assert catalog["place"] == {"city": None, "subdivision": None, "country": None}
    assert catalog["languages"] == catalog["subdivisions"] == catalog["publication_types"] == []


async def test_a_viewer_cannot_create(admin_client: AsyncClient, editor: str) -> None:
    response = await create(admin_client, new_catalog(), email=VIEWER)

    assert response.status_code == 403
    assert problem_type(response) == "forbidden"


async def test_another_catalogs_identity_href_is_a_duplicate(
    admin_client: AsyncClient, editor: str, db_session: AsyncSession
) -> None:
    taken = (
        await db_session.execute(
            text("SELECT href FROM links WHERE rel = 'catalog' ORDER BY href LIMIT 1")
        )
    ).scalar_one()
    before = len(await all_catalog_ids(db_session))

    response = await create(admin_client, new_catalog(links=[{"rel": "catalog", "href": taken}]))

    assert response.status_code == 409, response.text
    assert problem_type(response) == "duplicate-catalog"
    assert len(await all_catalog_ids(db_session)) == before


async def test_creating_the_same_catalog_twice_is_a_duplicate(
    admin_client: AsyncClient, editor: str
) -> None:
    assert (await create(admin_client, new_catalog())).status_code == 201

    response = await create(admin_client, new_catalog(name="Again"))

    assert response.status_code == 409
    assert problem_type(response) == "duplicate-catalog"
    assert response.json()["detail"] == "Another catalog already has this catalog or shelf link."


async def test_an_address_too_long_for_the_index_is_named_not_a_server_error(
    admin_client: AsyncClient, editor: str
) -> None:
    wide = "https://example.org/" + "漢" * 2000

    response = await create(admin_client, new_catalog(links=[{"rel": "catalog", "href": wide}]))

    assert error_fields(response) == ["links.0.href"]
    assert response.json()["errors"][0]["message"] == "The address is too long."


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        (
            {"country": "FR", "subdivisions": ["BE-WAL"]},
            "subdivisions.0",
            "BE-WAL is not in this catalog's country",
        ),
        (
            {"country": None, "subdivisions": ["BE-WAL"]},
            "country",
            "Choose the country of these subdivisions first",
        ),
    ],
)
async def test_a_subdivision_must_be_in_the_catalogs_country(
    admin_client: AsyncClient, editor: str, changes: dict[str, Any], field: str, message: str
) -> None:
    response = await create(admin_client, new_catalog(**changes))

    assert error_fields(response) == [field]
    assert response.json()["errors"][0]["message"] == message


@pytest.mark.parametrize(
    ("changes", "fields"),
    [
        ({"name": "   "}, ["name"]),
        ({"name": "nul\x00"}, ["name"]),
        ({"kinds": []}, ["kinds"]),
        ({"kinds": ["nope"]}, ["kinds.0"]),
        ({"kinds": ["public", "public"]}, ["kinds.1"]),
        ({"color": "teal"}, ["color"]),
        ({"coverage": "planet"}, ["coverage"]),
        ({"country": "be"}, ["country"]),
        ({"country": "ZZ"}, ["country"]),
        ({"subdivisions": ["BE-XYZ"]}, ["subdivisions.0"]),
        ({"subdivisions": ["BE-WAL", "be-vlg"]}, ["subdivisions.1"]),
        ({"languages": ["not a tag!"]}, ["languages.0"]),
        ({"languages": ["fr", "FR"]}, ["languages.1"]),
        ({"publication_types": ["ebook", "ebook"]}, ["publication_types.1"]),
        ({"links": []}, ["links"]),
        ({"links": [{"rel": "icon", "href": "https://example.org/i.png"}]}, ["links"]),
        ({"links": [{"rel": "catalog", "href": "javascript:alert(1)"}]}, ["links.0.href"]),
        ({"links": [{"rel": "catalog", "href": "/relative"}]}, ["links.0.href"]),
        ({"links": [{"rel": "self", "href": HREF}]}, ["links.0.rel"]),
        ({"links": [{"rel": "catalog", "href": HREF, "templated": True}]}, ["links.0.templated"]),
        (
            {"links": [{"rel": "catalog", "href": HREF}, {"rel": "catalog", "href": HREF}]},
            ["links"],
        ),
        (
            {"links": [{"rel": "catalog", "href": HREF}, {"rel": "shelf", "href": HREF}]},
            ["links.1.href"],
        ),
        (
            {
                "links": [
                    {"rel": "catalog", "href": HREF},
                    {"rel": "catalog", "href": HREF, "title": "T"},
                ]
            },
            ["links.1.href"],
        ),
        ({"unknown": 1}, ["unknown"]),
    ],
)
async def test_an_invalid_field_is_named(
    admin_client: AsyncClient, editor: str, changes: dict[str, Any], fields: list[str]
) -> None:
    response = await create(admin_client, new_catalog(**changes))

    assert error_fields(response) == fields
    assert all(error["message"] for error in response.json()["errors"])


async def test_required_fields_are_named_when_missing(
    admin_client: AsyncClient, editor: str
) -> None:
    assert sorted(error_fields(await create(admin_client, {}))) == ["kinds", "links", "name"]


async def test_a_lowercase_country_says_what_it_should_be(
    admin_client: AsyncClient, editor: str
) -> None:
    response = await create(admin_client, new_catalog(country="be"))

    assert response.json()["errors"] == [
        {"field": "country", "message": "country 'be' should be 'BE'"}
    ]


# --- edit ----------------------------------------------------------------------------------------


async def test_every_seeded_catalog_saves_unchanged_from_the_edit_form(
    admin_client: AsyncClient, editor: str, db_session: AsyncSession
) -> None:
    """The Edit form sends every field back, so each seeded catalog must pass the create checks
    as it is stored, and a save that changes nothing keeps the version."""
    await seed_catalogs(db_session, REPO_ROOT / "data" / "dev-sample.json", recommended=False)
    await db_session.commit()
    ids = await all_catalog_ids(db_session)
    assert len(ids) >= 20
    for catalog_id in ids:
        detail = (await read(admin_client, catalog_id)).json()
        if detail["status"] == "archived":
            continue
        response = await edit(admin_client, catalog_id, form_body(detail))
        assert response.status_code == 200, (catalog_id, response.text)
        assert response.json()["version"] == detail["version"], catalog_id


async def test_an_edit_changes_what_was_sent_and_bumps_the_version(
    admin_client: AsyncClient, editor: str
) -> None:
    catalog = (await create(admin_client, new_catalog())).json()

    response = await edit(
        admin_client, catalog["id"], {"version": 1, "name": "Renamed", "description": "Now set"}
    )

    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["version"] == 2
    assert edited["name"] == "Renamed"
    assert edited["description"] == "Now set"
    unchanged = ("kinds", "color", "coverage", "place", "subdivisions", "links", "languages")
    assert {key: edited[key] for key in unchanged} == {key: catalog[key] for key in unchanged}


async def test_null_clears_an_optional_field_and_a_list_replaces_the_list(
    admin_client: AsyncClient, editor: str
) -> None:
    catalog = (await create(admin_client, new_catalog())).json()

    response = await edit(
        admin_client,
        catalog["id"],
        {
            "version": 1,
            "city": None,
            "coverage": None,
            "country": None,
            "subdivisions": [],
            "kinds": ["school", "open"],
            "links": [{"rel": "catalog", "href": HREF}],
        },
    )

    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["place"] == {"city": None, "subdivision": None, "country": None}
    assert edited["coverage"] is None
    assert edited["subdivisions"] == []
    assert sorted(edited["kinds"]) == ["open", "school"]
    assert [(link["rel"], link["href"]) for link in edited["links"]] == [("catalog", HREF)]


async def test_an_edit_that_changes_nothing_keeps_the_version(
    admin_client: AsyncClient, editor: str
) -> None:
    catalog = (await create(admin_client, new_catalog())).json()
    body = form_body(catalog)
    body["languages"] = ["PT-br", "fr"]
    body["links"].reverse()

    response = await edit(admin_client, catalog["id"], body)

    assert response.status_code == 200
    assert response.json()["version"] == 1


@pytest.mark.parametrize("field", ["name", "color", "kinds", "links", "subdivisions", "languages"])
async def test_a_required_field_or_a_list_cannot_be_null(
    admin_client: AsyncClient, editor: str, field: str
) -> None:
    catalog = (await create(admin_client, new_catalog())).json()

    response = await edit(admin_client, catalog["id"], {"version": 1, field: None})

    assert error_fields(response) == [field]


async def test_an_edit_is_checked_whole_like_a_new_catalog(
    admin_client: AsyncClient, editor: str
) -> None:
    catalog = (await create(admin_client, new_catalog())).json()

    response = await edit(admin_client, catalog["id"], {"version": 1, "country": "FR"})
    assert error_fields(response) == ["subdivisions.0"]

    response = await edit(
        admin_client, catalog["id"], {"version": 1, "country": "FR", "subdivisions": []}
    )
    assert response.status_code == 200
    response = await edit(
        admin_client, catalog["id"], {"version": 2, "links": [{"rel": "icon", "href": HREF}]}
    )
    assert error_fields(response) == ["links"]


async def test_an_edit_needs_the_current_version(
    admin_client: AsyncClient, editor: str, db_session: AsyncSession
) -> None:
    catalog = (await create(admin_client, new_catalog())).json()
    assert (await edit(admin_client, catalog["id"], {"version": 1, "name": "A"})).status_code == 200

    response = await edit(admin_client, catalog["id"], {"version": 1, "name": "B"})

    assert response.status_code == 409
    assert problem_type(response) == "version-conflict"
    assert (await read(admin_client, catalog["id"])).json()["name"] == "A"


async def test_an_archived_catalog_is_read_only(
    admin_client: AsyncClient, editor: str, db_session: AsyncSession
) -> None:
    catalog = (await create(admin_client, new_catalog(publish=True))).json()
    archived = await transition(admin_client, catalog["id"], {"action": "archive", "version": 1})
    assert archived.status_code == 200

    response = await edit(admin_client, catalog["id"], {"version": 2, "name": "Changed"})

    assert response.status_code == 409
    assert problem_type(response) == "archived-read-only"
    assert await status_and_version(db_session, catalog["id"]) == ("archived", 2)


async def test_an_edit_cannot_take_another_catalogs_identity_href(
    admin_client: AsyncClient, editor: str, db_session: AsyncSession
) -> None:
    taken = (
        await db_session.execute(
            text("SELECT href FROM links WHERE rel = 'catalog' ORDER BY href LIMIT 1")
        )
    ).scalar_one()
    catalog = (await create(admin_client, new_catalog())).json()

    response = await edit(
        admin_client, catalog["id"], {"version": 1, "links": [{"rel": "catalog", "href": taken}]}
    )

    assert response.status_code == 409
    assert problem_type(response) == "duplicate-catalog"


async def test_editing_a_missing_or_malformed_id_is_not_found(
    admin_client: AsyncClient, editor: str
) -> None:
    for catalog_id in (str(uuid.uuid4()), "not-a-uuid"):
        response = await edit(admin_client, catalog_id, {"version": 1, "name": "X"})
        assert response.status_code == 404
        assert problem_type(response) == "catalog-not-found"


async def test_a_viewer_cannot_edit(admin_client: AsyncClient, editor: str) -> None:
    catalog = (await create(admin_client, new_catalog())).json()

    response = await edit(admin_client, catalog["id"], {"version": 1, "name": "X"}, email=VIEWER)

    assert response.status_code == 403


# --- delete --------------------------------------------------------------------------------------


async def test_a_draft_is_deleted_for_good(
    admin_client: AsyncClient, editor: str, db_session: AsyncSession
) -> None:
    catalog = (await create(admin_client, new_catalog())).json()

    response = await delete(admin_client, catalog["id"], 1)

    assert response.status_code == 204
    assert response.content == b""
    assert (await read(admin_client, catalog["id"])).status_code == 404
    links = await db_session.execute(
        text("SELECT count(*) FROM links WHERE href = :h"), {"h": HREF}
    )
    assert links.scalar_one() == 0
    assert (await create(admin_client, new_catalog())).status_code == 201


@pytest.mark.parametrize("action", [None, "unpublish", "archive"])
async def test_only_a_draft_can_be_deleted(
    admin_client: AsyncClient, editor: str, db_session: AsyncSession, action: str | None
) -> None:
    catalog = (await create(admin_client, new_catalog(publish=True))).json()
    version = 1
    if action:
        assert (
            await transition(admin_client, catalog["id"], {"action": action, "version": 1})
        ).status_code == 200
        version = 2

    response = await delete(admin_client, catalog["id"], version)

    assert response.status_code == 409
    assert problem_type(response) == "not-deletable"
    assert (await read(admin_client, catalog["id"])).status_code == 200


async def test_a_delete_needs_the_current_version(admin_client: AsyncClient, editor: str) -> None:
    catalog = (await create(admin_client, new_catalog())).json()
    assert (await edit(admin_client, catalog["id"], {"version": 1, "name": "A"})).status_code == 200

    response = await delete(admin_client, catalog["id"], 1)

    assert response.status_code == 409
    assert problem_type(response) == "version-conflict"


@pytest.mark.parametrize("version", [None, "abc", 0])
async def test_a_delete_without_a_valid_version_is_refused(
    admin_client: AsyncClient, editor: str, version: Any
) -> None:
    catalog = (await create(admin_client, new_catalog())).json()
    params = {} if version is None else {"version": version}

    response = await admin_client.delete(
        f"/admin/catalogs/{catalog['id']}", params=params, headers=admin_headers(EDITOR)
    )

    assert error_fields(response) == ["version"]


async def test_a_viewer_cannot_delete(admin_client: AsyncClient, editor: str) -> None:
    catalog = (await create(admin_client, new_catalog())).json()

    assert (await delete(admin_client, catalog["id"], 1, email=VIEWER)).status_code == 403


# --- allowed_actions and the full loop -----------------------------------------------------------


@pytest.mark.parametrize("action", [None, "publish", "unpublish", "archive"])
async def test_allowed_actions_match_what_edit_and_delete_accept(
    admin_client: AsyncClient, editor: str, action: str | None
) -> None:
    catalog = (await create(admin_client, new_catalog())).json()
    version = 1
    for step in {"publish": ["publish"], "unpublish": ["publish", "unpublish"]}.get(
        action or "", ["publish", "archive"] if action == "archive" else []
    ):
        assert (
            await transition(admin_client, catalog["id"], {"action": step, "version": version})
        ).status_code == 200
        version += 1
    allowed = (await read(admin_client, catalog["id"])).json()["allowed_actions"]

    edited = await edit(admin_client, catalog["id"], {"version": version, "name": "Checked"})
    assert (edited.status_code == 200) == ("edit" in allowed)
    if edited.status_code == 200:
        version += 1
    deleted = await delete(admin_client, catalog["id"], version)
    assert (deleted.status_code == 204) == ("delete" in allowed)


async def test_the_editor_loop(admin_client: AsyncClient, editor: str) -> None:
    """Add as a draft and delete it; add, publish, edit, archive, and the edit is refused."""
    draft = (await create(admin_client, new_catalog())).json()
    assert (await delete(admin_client, draft["id"], draft["version"])).status_code == 204

    catalog = (await create(admin_client, new_catalog())).json()
    published = await transition(admin_client, catalog["id"], {"action": "publish", "version": 1})
    assert published.json()["catalog"]["status"] == "published"
    edited = await edit(admin_client, catalog["id"], {"version": 2, "city": "Namur"})
    assert edited.json()["place"]["city"] == "Namur"
    archived = await transition(admin_client, catalog["id"], {"action": "archive", "version": 3})
    assert archived.json()["catalog"]["allowed_actions"] == ["unarchive"]
    refused = await edit(admin_client, catalog["id"], {"version": 4, "city": "Liège"})
    assert problem_type(refused) == "archived-read-only"


async def test_the_openapi_document_has_the_write_routes(admin_client: AsyncClient) -> None:
    paths = (await admin_client.get("/openapi.json")).json()["paths"]

    assert {"post"} <= set(paths["/admin/catalogs"])
    assert {"get", "patch", "delete"} <= set(paths["/admin/catalogs/{catalog_id}"])


@pytest.mark.parametrize(
    "city",
    ["Saint-Étienne", "L'Aquila", "L\u2019Aquila", "St. Gallen", "Tiruchirappalli", "東京", "मुंबई"],
)
async def test_a_city_in_letters_is_accepted(
    admin_client: AsyncClient, editor: str, city: str
) -> None:
    response = await create(admin_client, new_catalog(city=f"  {city} "))

    assert response.status_code == 201, response.text
    assert response.json()["place"]["city"] == city


@pytest.mark.parametrize(
    ("city", "message"),
    [
        ("Paris 75", "Use letters only: no numbers or symbols."),
        ("Frankfurt (Oder)", "Use letters only: no numbers or symbols."),
        ("Namur/Liège", "Use letters only: no numbers or symbols."),
        ("-Lyon", "Use letters only: no numbers or symbols."),
        ("Two  Spaces", "Use letters only: no numbers or symbols."),
        ("   ", "The city cannot be blank."),
    ],
)
async def test_a_city_with_numbers_or_symbols_is_refused(
    admin_client: AsyncClient, editor: str, city: str, message: str
) -> None:
    response = await create(admin_client, new_catalog(city=city))

    assert response.json()["errors"] == [{"field": "city", "message": message}]


async def test_an_edit_checks_the_city_too(admin_client: AsyncClient, editor: str) -> None:
    catalog = (await create(admin_client, new_catalog())).json()

    response = await edit(admin_client, catalog["id"], {"version": 1, "city": "Liège 4000"})

    assert error_fields(response) == ["city"]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"name": "x" * 201}, "name"),
        ({"name": "tab\there"}, "name"),
        ({"description": "d" * 2001}, "description"),
        ({"city": "c" * 101}, "city"),
        ({"links": [{"rel": "catalog", "href": HREF, "type": "t" * 256}]}, "links.0.type"),
        ({"links": [{"rel": "catalog", "href": HREF, "title": "t" * 301}]}, "links.0.title"),
    ],
)
async def test_the_length_limits_match_the_back_office_form(
    admin_client: AsyncClient, editor: str, changes: dict[str, Any], field: str
) -> None:
    assert error_fields(await create(admin_client, new_catalog(**changes))) == [field]


async def test_a_multi_line_description_is_accepted(admin_client: AsyncClient, editor: str) -> None:
    response = await create(admin_client, new_catalog(description="First line.\n\tSecond line."))

    assert response.status_code == 201, response.text
