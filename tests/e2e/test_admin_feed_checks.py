"""`POST /admin/feed-checks` (contract 0.8): through the app, with the library servers mocked.

The app's fetcher is swapped for the real `SafeHttpClient` on an `httpx.MockTransport`, so the URL
policy, redirects, caps and retries all run; only the sockets are fake.
"""

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.network.safe_client import SafeHttpClient
from tests.conftest import admin_headers
from tests.e2e.test_admin_api import become

pytestmark = pytest.mark.e2e

EDITOR = "editor@edrlab.org"
VIEWER = "viewer@edrlab.org"
FEED = "https://feeds.example.org/opds"
PROBLEMS = "https://registry.thoriumreader.com/problems/"


def opds_feed(title: str = "Example Library") -> dict[str, Any]:
    return {
        "metadata": {"title": title, "language": "en"},
        "links": [
            {"rel": "self", "href": FEED},
            {"rel": "search", "href": "/opds/search{?query}", "templated": True},
            {"rel": "http://opds-spec.org/shelf", "href": "/shelf"},
        ],
        "navigation": [],
    }


def library(request: httpx.Request) -> httpx.Response:
    """The mocked internet: one OPDS feed, its links, a web page, an Atom feed and a redirect."""
    url = str(request.url)
    routes: dict[str, httpx.Response] = {
        FEED: httpx.Response(
            200, json=opds_feed(), headers={"content-type": "application/opds+json"}
        ),
        "https://feeds.example.org/opds/search": httpx.Response(200),
        "https://feeds.example.org/shelf": httpx.Response(401),
        "https://www.example.org/": httpx.Response(
            200,
            content=b"<html lang='fr'><head><title>La Bib</title></head><body>",
            headers={"content-type": "text/html; charset=utf-8"},
        ),
        "https://feeds.example.org/atom": httpx.Response(
            200, content=b"<feed>", headers={"content-type": "application/atom+xml"}
        ),
        "https://old.example.org/opds": httpx.Response(301, headers={"location": FEED}),
    }
    return routes.get(url, httpx.Response(404))


@pytest.fixture
async def editor(admin_app: FastAPI, db_session: AsyncSession, searchable_catalogs: None) -> str:
    await become(db_session, EDITOR, "editor")
    admin_app.state.open_feed_fetcher = lambda: SafeHttpClient(
        transport=httpx.MockTransport(library)
    )
    return EDITOR


async def check(client: AsyncClient, body: Any, email: str = EDITOR) -> Response:
    return await client.post("/admin/feed-checks", json=body, headers=admin_headers(email))


async def test_a_feed_is_checked_and_its_fields_come_back(
    admin_client: AsyncClient, editor: str
) -> None:
    response = await check(admin_client, {"url": FEED})

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    report = response.json()
    assert report["verdict"] == "ok"
    assert report["format"] == "opds2"
    assert [(s["key"], s["state"]) for s in report["stages"]] == [
        ("address", "passed"),
        ("fetch", "passed"),
        ("feed", "passed"),
        ("links", "passed"),
    ]
    assert report["feed"]["name"] == "Example Library"
    assert [(link["rel"], link["href"], link["active"]) for link in report["feed"]["links"]] == [
        ("catalog", FEED, True),
        ("search", "https://feeds.example.org/opds/search{?query}", True),
        ("shelf", "https://feeds.example.org/shelf", True),
    ]
    assert [f["code"] for f in report["findings"]] == ["link-needs-sign-in"]
    assert report["duplicate"] is None
    assert "changes" not in report or report["changes"] is None


async def test_the_report_saves_as_a_draft(admin_client: AsyncClient, editor: str) -> None:
    """The loop the Add page runs: check, then send the feed fields and the editorial ones."""
    feed = (await check(admin_client, {"url": FEED})).json()["feed"]
    links = [
        {key: value for key, value in link.items() if key != "active" and value is not None}
        for link in feed["links"]
        if link["active"] is not False
    ]

    response = await admin_client.post(
        "/admin/catalogs",
        json={
            "name": feed["name"],
            "description": feed["description"],
            "languages": feed["languages"],
            "links": links,
            "kinds": ["public"],
        },
        headers=admin_headers(EDITOR),
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "draft"
    again = (await check(admin_client, {"url": FEED})).json()
    assert again["verdict"] == "blocked"
    assert again["duplicate"]["id"] == response.json()["id"]
    assert [f["code"] for f in again["findings"]] == ["duplicate-catalog"]


async def test_a_web_page_is_accepted(admin_client: AsyncClient, editor: str) -> None:
    report = (await check(admin_client, {"url": "https://www.example.org/"})).json()

    assert report["verdict"] == "ok"
    assert report["format"] == "html"
    assert report["feed"]["name"] == "La Bib"
    assert report["feed"]["languages"] == ["fr"]
    assert report["feed"]["links"][0]["type"] == "text/html"


async def test_an_atom_feed_is_blocked(admin_client: AsyncClient, editor: str) -> None:
    report = (await check(admin_client, {"url": "https://feeds.example.org/atom"})).json()

    assert report["verdict"] == "blocked"
    assert report["feed"] is None
    assert report["format"] is None
    assert [f["code"] for f in report["findings"]] == ["atom-feed"]


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/admin",
        "file:///etc/passwd",
    ],
)
async def test_an_unsafe_address_is_blocked_before_any_request(
    admin_client: AsyncClient, editor: str, url: str
) -> None:
    report = (await check(admin_client, {"url": url})).json()

    assert report["verdict"] == "blocked"
    assert report["findings"][0]["code"] == "url-unsafe"
    assert [s["state"] for s in report["stages"]] == ["failed", "skipped", "skipped", "skipped"]


async def test_a_permanent_redirect_saves_the_new_address(
    admin_client: AsyncClient, editor: str
) -> None:
    report = (await check(admin_client, {"url": "https://old.example.org/opds"})).json()

    assert report["url"] == "https://old.example.org/opds"
    assert report["final_url"] == FEED
    assert report["feed"]["links"][0]["href"] == FEED
    assert "redirects-permanently" in [f["code"] for f in report["findings"]]


async def test_a_seeded_catalogs_feed_is_a_duplicate(
    admin_client: AsyncClient, editor: str, db_session: AsyncSession
) -> None:
    row = (
        await db_session.execute(
            text(
                "SELECT c.id::text, c.title, l.href FROM links l JOIN catalogs c ON c.id ="
                " l.catalog_id WHERE l.rel = 'catalog' ORDER BY l.href LIMIT 1"
            )
        )
    ).one()

    report = (await check(admin_client, {"url": row.href})).json()

    assert report["duplicate"] == {"id": row.id, "name": row.title}
    assert report["verdict"] == "blocked"


async def test_a_re_check_says_what_changed(
    admin_client: AsyncClient, editor: str, admin_app: FastAPI
) -> None:
    feed = (await check(admin_client, {"url": FEED})).json()["feed"]
    links = [
        {key: value for key, value in link.items() if key != "active" and value is not None}
        for link in feed["links"]
    ]
    created = await admin_client.post(
        "/admin/catalogs",
        json={"name": "Old Name", "languages": ["fr"], "links": links[:2], "kinds": ["public"]},
        headers=admin_headers(EDITOR),
    )
    catalog_id = created.json()["id"]

    report = (await check(admin_client, {"url": FEED, "catalog_id": catalog_id})).json()

    assert report["duplicate"] is None
    changes = {change["field"]: change for change in report["changes"]}
    assert changes["name"] == {"field": "name", "catalog": "Old Name", "feed": "Example Library"}
    assert changes["languages"] == {"field": "languages", "catalog": ["fr"], "feed": ["en"]}
    assert [link["rel"] for link in changes["links"]["added"]] == ["shelf"]
    assert changes["links"]["removed"] == []


@pytest.mark.parametrize("catalog_id", ["00000000-0000-0000-0000-000000000000", "nope"])
async def test_re_checking_a_missing_catalog_is_not_found(
    admin_client: AsyncClient, editor: str, catalog_id: str
) -> None:
    response = await check(admin_client, {"url": FEED, "catalog_id": catalog_id})

    assert response.status_code == 404
    assert response.json()["type"] == PROBLEMS + "catalog-not-found"


async def test_a_viewer_cannot_check(admin_client: AsyncClient, editor: str) -> None:
    response = await check(admin_client, {"url": FEED}, email=VIEWER)

    assert response.status_code == 403


@pytest.mark.parametrize("body", [{}, {"url": ""}, {"url": "x" * 3000}, {"url": FEED, "x": 1}])
async def test_a_malformed_body_is_422(admin_client: AsyncClient, editor: str, body: Any) -> None:
    response = await check(admin_client, body)

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "validation"


async def test_a_full_instance_answers_busy(
    admin_client: AsyncClient, editor: str, admin_app: FastAPI
) -> None:
    slots = admin_app.state.feed_check_slots
    held = 0
    while not slots.locked():
        await slots.acquire()
        held += 1
    try:
        response = await check(admin_client, {"url": FEED})
    finally:
        for _ in range(held):
            slots.release()

    assert response.status_code == 429
    assert response.json()["type"] == PROBLEMS + "checks-busy"
    assert int(response.headers["retry-after"]) > 0


async def test_the_route_is_in_the_openapi_document(admin_client: AsyncClient) -> None:
    schema = (await admin_client.get("/openapi.json")).json()

    assert "post" in schema["paths"]["/admin/feed-checks"]
    report = schema["components"]["schemas"]["FeedCheckResponse"]
    assert {"verdict", "format", "stages", "findings", "feed", "duplicate"} <= set(
        report["required"]
    )
    assert json.dumps(report)
