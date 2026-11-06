"""A read-only service key: it reads the admin API and runs feed checks, and nothing else."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from registry.network.safe_client import SafeHttpClient
from tests.conftest import ADMIN_SERVICE_KEY
from tests.e2e.test_admin_api import become
from tests.unit.test_safe_client import StreamingTransport

pytestmark = pytest.mark.e2e

READ_ONLY_KEY = "read-only-key-for-tests"
EDITOR = "editor@edrlab.org"


@pytest.fixture
async def read_only(admin_app: FastAPI, db_session: AsyncSession) -> None:
    await become(db_session, EDITOR, "editor")
    admin_app.state.settings = admin_app.state.settings.model_copy(
        update={"read_only_service_keys": SecretStr(READ_ONLY_KEY)}
    )
    admin_app.state.open_feed_fetcher = lambda: SafeHttpClient(
        transport=StreamingTransport(lambda _: httpx.Response(404))
    )


def headers(key: str = READ_ONLY_KEY) -> dict[str, str]:
    return {"X-Registry-Service-Key": key, "X-Registry-Dev-Identity": EDITOR}


@pytest.mark.parametrize("path", ["/admin/me", "/admin/stats", "/admin/catalogs", "/admin/users"])
async def test_a_read_only_key_reads(admin_client: AsyncClient, read_only: None, path: str) -> None:
    assert (await admin_client.get(path, headers=headers())).status_code == 200


async def test_a_read_only_key_runs_a_feed_check(
    admin_client: AsyncClient, read_only: None
) -> None:
    response = await admin_client.post(
        "/admin/feed-checks", json={"url": "https://feeds.example.org/opds"}, headers=headers()
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/admin/catalogs", {"name": "x", "kinds": ["open"], "links": []}),
        ("PATCH", "/admin/me", {"name": "x"}),
        ("PATCH", "/admin/catalogs/00000000-0000-0000-0000-000000000000", {"version": 1}),
        ("DELETE", "/admin/catalogs/00000000-0000-0000-0000-000000000000?version=1", None),
        ("POST", "/admin/catalogs/00000000-0000-0000-0000-000000000000/transitions", {}),
        ("PUT", "/admin/countries/IN/subdivision-types", {"keys": []}),
    ],
)
async def test_a_read_only_key_cannot_write(
    admin_client: AsyncClient, read_only: None, method: str, path: str, body: Any
) -> None:
    response = await admin_client.request(method, path, json=body, headers=headers())

    assert response.status_code == 403
    assert response.json()["detail"] == "This key can only read."


async def test_the_full_key_still_writes(admin_client: AsyncClient, read_only: None) -> None:
    response = await admin_client.patch(
        "/admin/me", json={"name": "Writer"}, headers=headers(ADMIN_SERVICE_KEY)
    )

    assert response.status_code == 200
