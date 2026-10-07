"""`/dev/fetch` outside local: who may call it, and where it may go (ADR-063).

The route makes the server request a URL a visitor names. On a public service that is an open relay
unless two things hold: the caller holds the shared secret, and the host is one the registry lists.
These tests mount only the fetch router, with a deployed environment, so they do not depend on how
`create_app` mounts it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.api.routes import dev_fetch
from registry.core.config import Settings
from registry.repositories.catalog_repository import CatalogRepository

pytestmark = pytest.mark.e2e

TOKEN = "s3cret-for-tests"
STRANGER = "https://not-a-registered-library.example/feed"


def deployed(settings: Settings, *, token: str | None = TOKEN) -> Settings:
    return settings.model_copy(
        update={
            "environment": "production",
            "dev_fetch_token": SecretStr(token) if token else None,
        }
    )


@pytest.fixture
async def make_client(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[object]:
    clients: list[AsyncClient] = []

    def build(configured: Settings) -> AsyncClient:
        app = FastAPI()
        app.include_router(dev_fetch.router)
        app.state.settings = configured

        @asynccontextmanager
        async def open_reader() -> AsyncIterator[CatalogRepository]:
            async with session_factory() as session:
                yield CatalogRepository(session)

        app.state.open_catalog_reader = open_reader
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        clients.append(client)
        return client

    yield build
    for client in clients:
        await client.aclose()


# --- who may call ---------------------------------------------------------------------------------


async def test_without_a_configured_token_the_route_does_not_exist(
    settings: Settings, make_client: object
) -> None:
    client = make_client(deployed(settings, token=None))  # type: ignore[operator]

    response = await client.get(
        "/dev/fetch", params={"url": STRANGER}, headers={"X-Dev-Token": TOKEN}
    )

    assert response.status_code == 404


@pytest.mark.parametrize("header", [None, "", "wrong", TOKEN + "x", TOKEN[:-1]])
async def test_a_missing_or_wrong_token_is_refused(
    settings: Settings, make_client: object, header: str | None
) -> None:
    client = make_client(deployed(settings))  # type: ignore[operator]
    headers = {} if header is None else {"X-Dev-Token": header}

    response = await client.get("/dev/fetch", params={"url": STRANGER}, headers=headers)

    assert response.status_code == 401
    assert TOKEN not in response.text


async def test_the_token_is_not_accepted_as_a_query_parameter(
    settings: Settings, make_client: object
) -> None:
    """A URL ends up in logs and history; the secret must only travel in a header."""
    client = make_client(deployed(settings))  # type: ignore[operator]

    response = await client.get("/dev/fetch", params={"url": STRANGER, "token": TOKEN})

    assert response.status_code == 401


def test_local_needs_no_token_and_is_not_restricted_to_registered_hosts(settings: Settings) -> None:
    local = settings.model_copy(update={"environment": "local"})

    assert dev_fetch.authorize_fetch(local, None) is False


def test_a_deployed_environment_with_the_right_token_is_restricted(settings: Settings) -> None:
    assert dev_fetch.authorize_fetch(deployed(settings), TOKEN) is True


# --- where it may go ------------------------------------------------------------------------------


async def test_a_valid_token_still_cannot_reach_an_unregistered_host(
    settings: Settings, make_client: object, searchable_catalogs: None
) -> None:
    client = make_client(deployed(settings))  # type: ignore[operator]

    response = await client.get(
        "/dev/fetch", params={"url": STRANGER}, headers={"X-Dev-Token": TOKEN}
    )

    assert response.status_code == 403
    assert "not a registered library" in response.json()["detail"]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/feed",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/",
        "file:///etc/passwd",
    ],
)
async def test_internal_addresses_are_refused_even_with_a_valid_token(
    settings: Settings, make_client: object, searchable_catalogs: None, url: str
) -> None:
    client = make_client(deployed(settings))  # type: ignore[operator]

    response = await client.get("/dev/fetch", params={"url": url}, headers={"X-Dev-Token": TOKEN})

    assert response.status_code in {403, 422}


async def test_the_registered_hosts_are_the_hosts_of_the_catalog_links(
    settings: Settings, make_client: object, searchable_catalogs: None
) -> None:
    client = make_client(deployed(settings))  # type: ignore[operator]
    request = type("R", (), {"app": client._transport.app})()

    hosts = await dev_fetch.read_registered_hosts(request)

    assert hosts, "the seeded libraries have catalog links"
    assert all(host == host.lower() and "/" not in host and ":" not in host for host in hosts)
    assert "not-a-registered-library.example" not in hosts


async def test_a_registered_host_is_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public(url: str) -> None:
        return None

    monkeypatch.setattr(dev_fetch, "require_public_host", public)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))

    status, _, body, _ = await dev_fetch.fetch_public(
        "https://library.example/opds",
        {},
        allowed_hosts=frozenset({"library.example"}),
        transport=transport,
    )

    assert status == 200
    assert b"ok" in body


async def test_a_registered_host_cannot_redirect_the_request_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def public(url: str) -> None:
        return None

    monkeypatch.setattr(dev_fetch, "require_public_host", public)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://elsewhere.example/x"})
    )

    with pytest.raises(HTTPException) as raised:
        await dev_fetch.fetch_public(
            "https://library.example/opds",
            {},
            allowed_hosts=frozenset({"library.example"}),
            transport=transport,
        )

    assert raised.value.status_code == 403


def test_host_matching_is_exact_not_a_suffix_match() -> None:
    allowed = frozenset({"library.example"})

    dev_fetch.require_registered_host("https://library.example/a", allowed)
    for url in (
        "https://evil-library.example/a",
        "https://library.example.evil.test/a",
        "https://sub.library.example/a",
        "https://library.example@evil.test/a",
    ):
        with pytest.raises(HTTPException):
            dev_fetch.require_registered_host(url, allowed)
