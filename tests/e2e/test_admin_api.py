"""`/admin`: who may call it, and what the read endpoints say.

The app is built as a developer runs it locally (a key, a dev identity). The token
path is covered in `tests/unit/test_admin_identity.py`.
"""

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from registry.core.constants import PROBLEM_JSON_MEDIA_TYPE
from tests.conftest import ADMIN_SERVICE_KEY, SEED_CATALOG_COUNT, admin_headers
from tests.search_helpers import ALL_TITLES, add_synthetic_catalogs

pytestmark = pytest.mark.e2e

PROBLEMS = "https://registry.thoriumreader.com/problems/"


async def become(db_session: AsyncSession, email: str, role: str) -> None:
    await db_session.execute(
        text(
            "INSERT INTO actors (email, role) VALUES (:e, CAST(:r AS actor_role)) "
            "ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role, is_active = true"
        ),
        {"e": email, "r": role},
    )
    await db_session.commit()


async def set_status(db_session: AsyncSession, title_like: str, status: str) -> None:
    await db_session.execute(
        text("UPDATE catalogs SET status = CAST(:s AS catalog_status) WHERE title ILIKE :t"),
        {"s": status, "t": title_like},
    )
    await db_session.commit()


async def list_catalogs(
    client: AsyncClient, email: str = "viewer@edrlab.org", **params: Any
) -> Any:
    response = await client.get("/admin/catalogs", params=params, headers=admin_headers(email))
    assert response.status_code == 200, response.text
    return response.json()


# --- authentication ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/admin/me", "/admin/stats", "/admin/catalogs", "/admin/filter-options"]
)
async def test_no_credentials_is_a_401_problem_document(
    admin_client: AsyncClient, path: str
) -> None:
    response = await admin_client.get(path)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert response.json()["type"] == f"{PROBLEMS}unauthorized"


async def test_a_wrong_service_key_is_a_401_even_with_an_identity(
    admin_client: AsyncClient,
) -> None:
    headers = {**admin_headers(), "X-Registry-Service-Key": "wrong"}

    assert (await admin_client.get("/admin/me", headers=headers)).status_code == 401


async def test_the_service_key_alone_is_not_an_identity(admin_client: AsyncClient) -> None:
    """A missing identity is a 401, not an outage."""
    response = await admin_client.get(
        "/admin/me", headers={"X-Registry-Service-Key": ADMIN_SERVICE_KEY}
    )

    assert response.status_code == 401


async def test_a_forged_token_is_a_401_not_a_503(admin_client: AsyncClient) -> None:
    """Locally no issuer is configured, so the token cannot be checked: still a 401."""
    headers = {"X-Registry-Service-Key": ADMIN_SERVICE_KEY, "Cf-Access-Jwt-Assertion": "a.b.c"}

    assert (await admin_client.get("/admin/me", headers=headers)).status_code == 401


async def test_an_unconfigured_deployment_answers_503_and_leaks_nothing(
    app: Any, client: AsyncClient
) -> None:
    # A developer's own .env may define keys; this test is about their absence.
    app.state.settings = app.state.settings.model_copy(update={"service_keys": None})

    response = await client.get("/admin/me", headers=admin_headers())

    assert response.status_code == 503
    assert response.json()["detail"] == "The admin API is not configured."


async def test_the_dev_identity_is_ignored_when_the_flag_is_off(
    admin_app: Any, admin_client: AsyncClient
) -> None:
    admin_app.state.settings = admin_app.state.settings.model_copy(
        update={"admin_dev_identity": False}
    )

    assert (await admin_client.get("/admin/me", headers=admin_headers())).status_code == 401


# --- RFC 9457 ----------------------------------------------------------------------------------


async def test_about_blank_problems_are_titled_with_the_status_phrase(
    admin_client: AsyncClient,
) -> None:
    missing = await admin_client.get("/admin/nope", headers=admin_headers())
    wrong_method = await admin_client.post("/admin/me", headers=admin_headers())

    assert (missing.status_code, missing.json()["title"], missing.json()["type"]) == (
        404,
        "Not Found",
        "about:blank",
    )
    assert (wrong_method.status_code, wrong_method.json()["title"]) == (405, "Method Not Allowed")
    assert missing.json()["detail"] == "No resource at /admin/nope."
    assert wrong_method.json()["detail"] == "POST is not allowed on /admin/me."
    assert "GET" in wrong_method.headers["allow"]
    assert wrong_method.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert wrong_method.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    [
        ("/admin/me", {}, 401),
        ("/admin/nope", admin_headers(), 404),
        ("/admin/catalogs?page=0", admin_headers(), 422),
    ],
)
async def test_every_problem_names_its_request_in_instance(
    admin_client: AsyncClient, path: str, headers: dict[str, str], status: int
) -> None:
    request_id = "6f1c1d0e-2b6a-4a57-9a0e-3d6f4a0d9b11"

    response = await admin_client.get(path, headers={**headers, "X-Request-Id": request_id})

    assert response.status_code == status
    assert response.json()["instance"] == f"urn:uuid:{request_id}"
    assert response.headers["x-request-id"] == request_id


async def test_instance_uses_the_minted_id_and_odd_ids_are_replaced(
    admin_client: AsyncClient,
) -> None:
    minted = await admin_client.get("/admin/me")
    odd = await admin_client.get("/admin/me", headers={"X-Request-Id": "a b/c"})

    assert minted.json()["instance"] == f"urn:uuid:{minted.headers['x-request-id']}"
    assert odd.headers["x-request-id"] != "a b/c"
    assert odd.json()["instance"] == f"urn:uuid:{odd.headers['x-request-id']}"


async def test_an_unhandled_error_is_a_fixed_problem_that_never_shows_the_exception(
    admin_app: Any,
) -> None:
    async def boom() -> None:
        raise RuntimeError("postgresql://user:hunter2@db/registry")

    admin_app.add_api_route("/admin/boom", boom)
    transport = ASGITransport(app=admin_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/admin/boom", headers={"X-Request-Id": "r-1"})

    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "detail": "An unexpected error occurred.",
        "instance": "urn:request-id:r-1",
    }
    assert "hunter2" not in response.text


async def test_a_401_says_how_to_authenticate(admin_client: AsyncClient) -> None:
    response = await admin_client.get("/admin/me")

    assert response.headers["www-authenticate"].startswith("Registry-Service-Key")


async def test_the_openapi_document_describes_every_admin_problem(
    admin_client: AsyncClient,
) -> None:
    document = (await admin_client.get("/openapi.json")).json()

    for path, operations in document["paths"].items():
        if not path.startswith("/admin"):
            continue
        responses = operations["get"]["responses"]
        for status in ("401", "403", "500", "503"):
            content = responses[status]["content"]
            assert content["application/problem+json"]["schema"]["$ref"].endswith(
                "/ProblemDocument"
            )
    catalogs = document["paths"]["/admin/catalogs"]["get"]["responses"]["422"]
    assert "application/problem+json" in catalogs["content"]
    assert "errors" in document["components"]["schemas"]["ProblemDocument"]["properties"]


# --- a deployed environment --------------------------------------------------------------------


ISSUER = "https://issuer.example.test"
AUDIENCE = "aud-tag"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def deployed(admin_app: Any) -> None:
    """Turn the local test app into a staging one: no dev identity, tokens checked with our key."""
    admin_app.state.settings = admin_app.state.settings.model_copy(
        update={
            "environment": "staging",
            "admin_dev_identity": False,
            "admin_token_issuer": ISSUER,
            "admin_jwks_url": f"{ISSUER}/keys",
            "admin_token_audience": AUDIENCE,
        }
    )

    async def resolve(_token: str) -> Any:
        return SIGNING_KEY.public_key()

    admin_app.state.resolve_admin_key = resolve


def access_headers(email: str) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": [AUDIENCE], "exp": now + 300, "email": email},
        SIGNING_KEY,
        algorithm="RS256",
    )
    return {"X-Registry-Service-Key": ADMIN_SERVICE_KEY, "Cf-Access-Jwt-Assertion": token}


async def count_actors(db_session: AsyncSession) -> int:
    return int((await db_session.execute(text("SELECT count(*) FROM actors"))).scalar_one())


async def test_the_dev_identity_header_is_ignored_in_a_deployed_environment(
    admin_app: Any, admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    deployed(admin_app)

    response = await admin_client.get("/admin/me", headers=admin_headers("x@evil.org"))

    assert response.status_code == 401
    assert await count_actors(db_session) == 0


async def test_the_dev_identity_is_ignored_once_an_issuer_is_configured(
    admin_app: Any, admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Even on a local instance with the flag on: an unverified email never outranks tokens."""
    admin_app.state.settings = admin_app.state.settings.model_copy(
        update={"admin_token_issuer": ISSUER, "admin_token_audience": AUDIENCE}
    )

    response = await admin_client.get("/admin/me", headers=admin_headers("x@evil.org"))

    assert response.status_code == 401
    assert await count_actors(db_session) == 0


async def test_a_dev_identity_header_does_not_override_a_real_token(
    admin_app: Any, admin_client: AsyncClient
) -> None:
    deployed(admin_app)
    headers = {**access_headers("real@edrlab.org"), "X-Registry-Dev-Identity": "x@evil.org"}

    response = await admin_client.get("/admin/me", headers=headers)

    assert response.json()["email"] == "real@edrlab.org"


async def test_a_token_from_outside_the_allowed_domains_is_a_403_and_creates_no_actor(
    admin_app: Any, admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    deployed(admin_app)

    response = await admin_client.get("/admin/me", headers=access_headers("x@evil.org"))

    assert response.status_code == 403
    assert await count_actors(db_session) == 0


async def test_a_valid_token_from_an_allowed_domain_creates_a_viewer(
    admin_app: Any, admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    deployed(admin_app)

    response = await admin_client.get("/admin/me", headers=access_headers("New@EDRLab.org"))

    assert response.status_code == 200
    assert response.json()["role"] == "viewer"
    assert await count_actors(db_session) == 1


async def test_a_problem_body_never_contains_a_service_key(admin_client: AsyncClient) -> None:
    headers = {"X-Registry-Service-Key": "super-secret-attempt"}

    response = await admin_client.get("/admin/me", headers=headers)

    assert "super-secret-attempt" not in response.text
    assert ADMIN_SERVICE_KEY not in response.text


# --- the person --------------------------------------------------------------------------------


async def test_a_first_visit_creates_a_viewer(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await admin_client.get("/admin/me", headers=admin_headers("New.Person@EDRLab.org"))

    assert response.status_code == 200
    assert response.json() == {
        "email": "new.person@edrlab.org",
        "name": "new.person",
        "role": "viewer",
        "permissions": ["backoffice.view", "catalog.view", "country.view"],
    }
    stored = (await db_session.execute(text("SELECT role::text, last_seen_at FROM actors"))).one()
    assert stored[0] == "viewer"
    assert stored[1] is not None


async def test_the_same_person_in_another_case_is_the_same_actor(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    await admin_client.get("/admin/me", headers=admin_headers("Case@edrlab.org"))
    await admin_client.get("/admin/me", headers=admin_headers("CASE@EDRLAB.ORG"))

    assert (await db_session.execute(text("SELECT count(*) FROM actors"))).scalar() == 1


async def test_a_role_change_applies_to_the_next_request(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = admin_headers("promoted@edrlab.org")
    assert (await admin_client.get("/admin/me", headers=headers)).json()["role"] == "viewer"

    await become(db_session, "promoted@edrlab.org", "admin")

    body = (await admin_client.get("/admin/me", headers=headers)).json()
    assert body["role"] == "admin"
    assert "user.change_role" in body["permissions"]


async def test_a_login_never_changes_a_role_or_a_name(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_session.execute(
        text("INSERT INTO actors (email, name, role) VALUES ('kept@edrlab.org', 'Kept', 'editor')")
    )
    await db_session.commit()

    body = (await admin_client.get("/admin/me", headers=admin_headers("kept@edrlab.org"))).json()

    assert (body["name"], body["role"]) == ("Kept", "editor")


async def test_a_deactivated_person_is_a_403_everywhere(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    await become(db_session, "gone@edrlab.org", "admin")
    await db_session.execute(
        text("UPDATE actors SET is_active = false WHERE email = 'gone@edrlab.org'")
    )
    await db_session.commit()

    for path in ("/admin/me", "/admin/stats", "/admin/catalogs"):
        response = await admin_client.get(path, headers=admin_headers("gone@edrlab.org"))
        assert response.status_code == 403
        assert response.json()["type"] == f"{PROBLEMS}forbidden"


# --- transport ---------------------------------------------------------------------------------


async def test_admin_responses_are_never_cached(admin_client: AsyncClient) -> None:
    ok = await admin_client.get("/admin/stats", headers=admin_headers())
    refused = await admin_client.get("/admin/stats")

    assert ok.headers["cache-control"] == "no-store"
    assert refused.headers["cache-control"] == "no-store"


async def test_admin_gets_no_cors_headers_but_the_public_api_still_does(
    admin_client: AsyncClient,
) -> None:
    origin = {"Origin": "https://evil.example"}
    admin = await admin_client.get("/admin/stats", headers={**admin_headers(), **origin})
    preflight = await admin_client.options(
        "/admin/stats", headers={**origin, "Access-Control-Request-Method": "GET"}
    )
    public = await admin_client.get("/health/live", headers=origin)

    assert not [h for h in admin.headers if h.lower().startswith("access-control-")]
    assert not [h for h in preflight.headers if h.lower().startswith("access-control-")]
    assert public.headers["access-control-allow-origin"] == "*"


async def test_the_request_id_is_echoed_or_minted(admin_client: AsyncClient) -> None:
    echoed = await admin_client.get("/admin/stats", headers={"X-Request-Id": "abc-123"})
    minted = await admin_client.get("/admin/stats")

    assert echoed.headers["x-request-id"] == "abc-123"
    assert len(minted.headers["x-request-id"]) == 36


async def test_a_bad_query_is_a_422_problem_that_names_the_field(admin_client: AsyncClient) -> None:
    response = await admin_client.get(
        "/admin/catalogs", params={"per_page": 101, "status": "bogus"}, headers=admin_headers()
    )

    body = response.json()
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert body["type"] == f"{PROBLEMS}validation"
    assert {error["field"] for error in body["errors"]} == {"per_page", "status.0"}
    assert response.headers["cache-control"] == "no-store"


async def test_a_page_past_the_cap_is_a_422_not_a_500(admin_client: AsyncClient) -> None:
    response = await admin_client.get(
        "/admin/catalogs", params={"page": 10**20}, headers=admin_headers()
    )

    assert response.status_code == 422
    assert [error["field"] for error in response.json()["errors"]] == ["page"]


async def test_a_long_search_text_is_cut_to_a_few_words(
    admin_client: AsyncClient, searchable_catalogs: None
) -> None:
    word = (await list_catalogs(admin_client))["items"][0]["name"].split()[0]

    body = await list_catalogs(admin_client, q=" ".join([word] * 8 + ["qqqqqq"]))

    assert body["total"] > 0


# --- stats -------------------------------------------------------------------------------------


async def test_stats_count_every_status_and_recommended_means_published_and_recommended(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await set_status(db_session, "%valais%", "draft")
    counts = {
        status: (
            await db_session.execute(
                text("SELECT count(*) FROM catalogs WHERE status = CAST(:s AS catalog_status)"),
                {"s": status},
            )
        ).scalar_one()
        for status in ("draft", "published", "unpublished", "archived")
    }
    recommended = (
        await db_session.execute(
            text("SELECT count(*) FROM catalogs WHERE status = 'published' AND recommended")
        )
    ).scalar_one()

    body = (await admin_client.get("/admin/stats", headers=admin_headers())).json()

    assert body == {"by_status": counts, "recommended": recommended}
    assert sum(counts.values()) == len(ALL_TITLES)
    assert counts["draft"] > 0
    assert counts["published"] > 0


# --- the list ----------------------------------------------------------------------------------


async def test_the_default_list_is_the_published_catalogs_by_name(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await set_status(db_session, "%valais%", "draft")

    body = await list_catalogs(admin_client)

    assert body["page"] == 1
    assert body["per_page"] == 100
    assert body["total"] == len(body["items"])
    assert {item["status"] for item in body["items"]} == {"published"}
    names = [item["name"].lower() for item in body["items"]]
    assert names == sorted(names)
    assert not [name for name in names if "valais" in name]


async def test_a_row_carries_everything_it_renders(
    admin_client: AsyncClient, searchable_catalogs: None
) -> None:
    items = (await list_catalogs(admin_client, q="paris"))["items"]

    assert items, "the seed has a Paris library"
    item = items[0]
    assert set(item) == {
        "id", "name", "host", "color", "icon_url", "kinds", "coverage", "place", "languages",
        "status", "recommended", "version", "allowed_actions", "public_url",
    }  # fmt: skip
    assert item["place"]["country"] == {"code": "FR", "name": "France"}
    assert item["host"] and "." in item["host"]
    assert item["public_url"] == f"http://testserver/catalogs/{item['id']}"
    assert item["version"] == 1
    assert {"tag": "fr", "name": "French"} in item["languages"]


async def test_allowed_actions_follow_the_role_and_the_status(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await become(db_session, "ed@edrlab.org", "editor")
    await set_status(db_session, "%valais%", "unpublished")

    viewer = (await list_catalogs(admin_client))["items"][0]
    editor = (await list_catalogs(admin_client, "ed@edrlab.org"))["items"][0]
    unpublished = (await list_catalogs(admin_client, "ed@edrlab.org", status="unpublished"))[
        "items"
    ]

    assert viewer["allowed_actions"] == []
    assert editor["allowed_actions"] == ["edit", "unpublish", "archive", "recommend"]
    assert unpublished[0]["allowed_actions"] == ["edit", "publish", "archive"]
    assert unpublished[0]["public_url"] is not None


async def test_a_draft_and_an_archived_catalog_have_no_public_url(
    admin_client: AsyncClient, db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await set_status(db_session, "%valais%", "draft")
    await set_status(db_session, "%paris%", "archived")

    items = (await list_catalogs(admin_client, status=["draft", "archived"]))["items"]

    assert {item["status"] for item in items} == {"draft", "archived"}
    assert all(item["public_url"] is None for item in items)


async def test_filters_combine_and_arrays_repeat(
    admin_client: AsyncClient, searchable_catalogs: None
) -> None:
    everything = await list_catalogs(admin_client, status=["published", "draft"])
    public_or_academic = await list_catalogs(admin_client, kind=["public", "academic"])
    french = await list_catalogs(admin_client, language=["FR"], country=["fr"])
    recommended = await list_catalogs(admin_client, recommended="true")

    assert everything["total"] >= public_or_academic["total"] > 0
    assert all({"public", "academic"} & set(item["kinds"]) for item in public_or_academic["items"])
    assert french["total"] > 0
    assert all(item["place"]["country"]["code"] == "FR" for item in french["items"])
    assert all(item["recommended"] for item in recommended["items"])
    assert 0 < recommended["total"] <= SEED_CATALOG_COUNT


async def test_search_matches_any_word_in_any_language_without_accents(
    admin_client: AsyncClient, searchable_catalogs: None
) -> None:
    by_english = await list_catalogs(admin_client, q="switzerland")
    by_native = await list_catalogs(admin_client, q="schweiz")
    accents = await list_catalogs(admin_client, q="MEDIATHEQUE")
    nothing = await list_catalogs(admin_client, q="zzzz-no-such-thing")

    assert by_english["total"] > 0
    assert by_native["total"] == by_english["total"]
    assert accents["total"] > 0
    assert nothing == {"items": [], "total": 0, "page": 1, "per_page": 100}


async def test_search_words_must_all_match(
    admin_client: AsyncClient, searchable_catalogs: None
) -> None:
    one = await list_catalogs(admin_client, q="switzerland")
    both = await list_catalogs(admin_client, q="switzerland zzzz-no-such-thing")

    assert one["total"] > 0
    assert both["total"] == 0


async def test_percent_and_underscore_in_the_search_are_plain_characters(
    admin_client: AsyncClient, searchable_catalogs: None
) -> None:
    assert (await list_catalogs(admin_client, q="%"))["total"] == 0
    assert (await list_catalogs(admin_client, q="_"))["total"] == 0


async def test_sorting_and_pages(admin_client: AsyncClient, searchable_catalogs: None) -> None:
    ascending = await list_catalogs(admin_client, sort="name", per_page=5)
    descending = await list_catalogs(admin_client, sort="-name", per_page=5)
    second = await list_catalogs(admin_client, sort="name", per_page=5, page=2)
    beyond = await list_catalogs(admin_client, sort="name", per_page=5, page=99)
    by_kind = await list_catalogs(admin_client, sort="kind")
    by_status = await list_catalogs(admin_client, sort="status", status=["published", "draft"])

    names = [item["name"] for item in ascending["items"]]
    assert len(names) == 5
    assert descending["items"][0]["name"] > names[-1]
    assert not {item["id"] for item in second["items"]} & {
        item["id"] for item in ascending["items"]
    }
    assert beyond["items"] == []
    assert beyond["total"] == ascending["total"] > 5  # the total survives a page past the end
    assert by_kind["total"] == ascending["total"]
    assert by_status["total"] == ascending["total"]


@pytest.mark.parametrize(
    "bad", [{"page": 0}, {"per_page": 0}, {"sort": "title"}, {"country": "FRA"}, {"coverage": "x"}]
)
async def test_invalid_parameters_are_refused(
    admin_client: AsyncClient, bad: dict[str, Any]
) -> None:
    response = await admin_client.get("/admin/catalogs", params=bad, headers=admin_headers())

    assert response.status_code == 422


async def test_the_list_is_one_statement_whatever_its_size(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    db_connection: AsyncConnection,
    searchable_catalogs: None,
) -> None:
    """R4. The login is one more statement; the list itself must not grow with the rows."""
    await become(db_session, "count@edrlab.org", "viewer")
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if "SAVEPOINT" not in statement:  # the harness wraps each commit in one
            seen.append(statement)

    event.listen(db_connection.sync_connection, "before_cursor_execute", record)
    try:
        await list_catalogs(admin_client, "count@edrlab.org")
        small = len(seen)
        seen.clear()
        await add_synthetic_catalogs(db_session, 60, "padding")
        seen.clear()
        big = await list_catalogs(admin_client, "count@edrlab.org")
        large = len(seen)
    finally:
        event.remove(db_connection.sync_connection, "before_cursor_execute", record)

    assert len(big["items"]) > 60
    assert small == large == 2  # actor login + the list


# --- filter options ----------------------------------------------------------------------------


async def test_filter_options_list_every_enumeration_with_labels(
    admin_client: AsyncClient, searchable_catalogs: None
) -> None:
    body = (await admin_client.get("/admin/filter-options", headers=admin_headers())).json()

    assert [o["value"] for o in body["statuses"]] == [
        "draft",
        "published",
        "unpublished",
        "archived",
    ]
    assert {"value": "country", "label": "National"} in body["coverages"]
    assert {"value": "specialized", "label": "Special libraries"} in body["kinds"]
    assert len(body["colors"]) == 8
    assert len(body["publication_types"]) == 7
    france = next(c for c in body["countries"] if c["code"] == "FR")
    assert france["name"] == "France"
    assert france["catalog_count"] > 0
    names = [c["name"].casefold() for c in body["countries"]]
    assert names == sorted(names)
    assert {"tag": "fr", "name": "French"}.items() <= next(
        lang for lang in body["languages"] if lang["tag"] == "fr"
    ).items()


async def test_filter_options_only_offer_places_in_use(admin_client: AsyncClient) -> None:
    body = (await admin_client.get("/admin/filter-options", headers=admin_headers())).json()

    assert body["countries"] == []
    assert body["languages"] == []


# --- the contract is published -----------------------------------------------------------------


async def test_the_openapi_document_describes_the_admin_routes(admin_client: AsyncClient) -> None:
    document = (await admin_client.get("/openapi.json")).json()

    assert {"/admin/me", "/admin/stats", "/admin/catalogs", "/admin/filter-options"} <= set(
        document["paths"]
    )
    status_enum = document["components"]["schemas"]["CatalogListItem"]["properties"]["status"]
    assert "CatalogStatus" in str(status_enum)
