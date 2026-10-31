"""Admin input, identity edge cases, redirects and request ids."""

import json
import time
from typing import Any

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry.api import admin_log
from tests.conftest import ADMIN_SERVICE_KEY, admin_headers
from tests.e2e.test_admin_api import AUDIENCE, ISSUER, SIGNING_KEY, count_actors, deployed
from tests.unit.test_admin_log import Lines

pytestmark = pytest.mark.e2e


def token_for(email: str, **extra: Any) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": [AUDIENCE], "exp": now + 300, "email": email, **extra}
    return {
        "X-Registry-Service-Key": ADMIN_SERVICE_KEY,
        "Cf-Access-Jwt-Assertion": jwt.encode(claims, SIGNING_KEY, algorithm="RS256"),
    }


@pytest.mark.parametrize(
    "params",
    [
        {"q": "par\x00is"},
        {"country": "\x00\x00"},
        {"language": "e\x00n"},
        {"q": "%FF%FE"},
        {"country": "f1"},
        {"language": "en us"},
    ],
    ids=[
        "null-in-q",
        "null-in-country",
        "null-in-language",
        "percent-garbage",
        "digit-country",
        "space-language",
    ],
)
async def test_odd_bytes_in_admin_filters_are_never_a_500(
    admin_client: AsyncClient, params: dict[str, str]
) -> None:
    response = await admin_client.get("/admin/catalogs", params=params, headers=admin_headers())

    assert response.status_code in (200, 422), response.text


async def test_refused_requests_write_nothing(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    for headers in (
        {},
        {"X-Registry-Service-Key": "wrong"},
        {"X-Registry-Service-Key": ADMIN_SERVICE_KEY},
    ):
        await admin_client.get("/admin/me", headers=headers)

    assert await count_actors(db_session) == 0


async def test_a_token_not_yet_valid_is_refused(admin_app: Any, admin_client: AsyncClient) -> None:
    deployed(admin_app)

    response = await admin_client.get(
        "/admin/me", headers=token_for("a@edrlab.org", nbf=int(time.time()) + 3600)
    )

    assert response.status_code == 401


#: "a@edrl" + CYRILLIC SMALL LETTER A + "b.org": looks like the allowed domain, is not.
HOMOGLYPH = "a@edrl\u0430b.org"


@pytest.mark.parametrize(
    "email",
    [
        "a@edrlab.org.",
        "a@edrlab.org.evil.com",
        "a@evil.com@edrlab.org",
        HOMOGLYPH,
        "a @edrlab.org",
        "a@edrlab.org ",
        "A@EDRLAB.ORG",
    ],
    ids=[
        "trailing-dot",
        "suffix-domain",
        "double-at",
        "cyrillic-a",
        "inner-space",
        "outer-space",
        "upper",
    ],
)
async def test_email_tricks(
    admin_app: Any, admin_client: AsyncClient, db_session: AsyncSession, email: str
) -> None:
    deployed(admin_app)

    response = await admin_client.get("/admin/me", headers=token_for(email))

    if email == "A@EDRLAB.ORG":
        assert response.status_code == 200 and response.json()["email"] == "a@edrlab.org"
    else:
        assert response.status_code in (401, 403), f"{email!r} -> {response.status_code}"


@pytest.mark.parametrize("supplied", ["r" * 129, "a\tb", "<script>", "a b"])
async def test_a_long_or_odd_request_id_is_replaced(
    admin_client: AsyncClient, supplied: str
) -> None:
    response = await admin_client.get("/nope", headers={"X-Request-Id": supplied})

    assert response.headers["x-request-id"] != supplied
    assert response.json()["instance"] == f"urn:uuid:{response.headers['x-request-id']}"


async def test_a_plain_request_id_is_echoed(admin_client: AsyncClient) -> None:
    response = await admin_client.get("/nope", headers={"X-Request-Id": "web-1:a.B_2"})

    assert response.headers["x-request-id"] == "web-1:a.B_2"


@pytest.mark.parametrize("path", ["/admin/me/", "/search/", "/catalogs/"])
async def test_a_trailing_slash_is_a_404_never_a_redirect(
    admin_client: AsyncClient, path: str
) -> None:
    response = await admin_client.get(path, headers={"host": "evil.example"})

    assert response.status_code == 404
    assert "location" not in response.headers


async def test_each_admin_request_by_an_actor_leaves_one_log_line(
    admin_client: AsyncClient,
) -> None:
    handler = Lines()
    admin_log._log.addHandler(handler)
    try:
        await admin_client.get("/admin/stats", headers={**admin_headers(), "X-Request-Id": "web-7"})
        await admin_client.get("/admin/stats")  # refused before any actor: not an audit line
    finally:
        admin_log._log.removeHandler(handler)

    audit = [line for line in handler.lines if line["message"] == "admin request"]
    assert len(audit) == 1
    assert audit[0]["status"] == 200
    assert audit[0]["request_id"] == "web-7"
    assert audit[0]["path"] == "/admin/stats"
    assert audit[0]["actor_id"]
    assert "@" not in json.dumps(audit[0])
