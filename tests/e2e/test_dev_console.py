"""The dev console exists on a developer's machine and nowhere else (ADR-063)."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.core.config import Settings
from registry.main import create_app

pytestmark = pytest.mark.e2e

SIMULATE = "/dev/simulate"


@pytest.fixture
async def local_client(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncClient]:
    built = create_app(settings.model_copy(update={"environment": "local"}))
    async with built.router.lifespan_context(built):
        built.state.session_factory = session_factory
        built.state.search_session_factory = session_factory
        async with AsyncClient(
            transport=ASGITransport(app=built), base_url="http://testserver"
        ) as client:
            yield client


@pytest.mark.parametrize("environment", ["test", "staging", "production"])
@pytest.mark.parametrize("path", ["/dev", "/dev/clients", "/dev/simulate?path=/&client=curl"])
async def test_it_is_not_mounted_outside_local(
    settings: Settings, environment: str, path: str
) -> None:
    built = create_app(settings.model_copy(update={"environment": environment}))
    async with AsyncClient(
        transport=ASGITransport(app=built), base_url="http://testserver"
    ) as client:
        assert (await client.get(path)).status_code == 404


async def test_the_page_is_served_locally_with_a_closed_policy(local_client: AsyncClient) -> None:
    response = await local_client.get("/dev")

    assert response.status_code == 200
    assert "Registry dev console" in response.text
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "connect-src 'self'" in policy


@pytest.mark.parametrize(
    ("client", "encoding"),
    [
        ("thorium", "br"),
        ("browser", "br"),
        ("urlsession", "gzip"),
        ("android", "gzip"),
        ("koreader", None),
        ("windows", None),
        ("curl", None),
    ],
)
async def test_each_client_gets_what_its_headers_negotiate(
    local_client: AsyncClient, searchable_catalogs: None, client: str, encoding: str | None
) -> None:
    response = await local_client.get(
        SIMULATE, params={"path": "/search?query=paris", "client": client}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == 200
    assert body["encoding"] == encoding
    assert body["is_json"] is True
    assert body["body"]["catalogs"]
    if encoding:
        assert body["wire_bytes"] < body["decoded_bytes"]
    else:
        assert body["wire_bytes"] == body["decoded_bytes"]


async def test_language_is_sent_as_accept_language(local_client: AsyncClient) -> None:
    response = await local_client.get(
        SIMULATE, params={"path": "/", "client": "curl", "language": "fr"}
    )

    assert response.json()["request"]["headers"]["Accept-Language"] == "fr"


@pytest.mark.parametrize(
    "path",
    ["/dev", "/health/live", "http://evil.example/", "//evil.example/", "/docs", "/search/../dev"],
)
async def test_only_the_public_reads_can_be_simulated(local_client: AsyncClient, path: str) -> None:
    response = await local_client.get(SIMULATE, params={"path": path, "client": "curl"})

    assert response.status_code == 422


async def test_an_unknown_client_is_rejected(local_client: AsyncClient) -> None:
    response = await local_client.get(SIMULATE, params={"path": "/", "client": "nope"})

    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/search", "/search?query=paris", "/"])
async def test_a_bare_search_path_is_allowed(local_client: AsyncClient, path: str) -> None:
    response = await local_client.get(SIMULATE, params={"path": path, "client": "curl"})

    assert response.status_code == 200
    assert response.json()["status"] == 200


async def test_every_client_says_whether_its_headers_were_measured(
    local_client: AsyncClient,
) -> None:
    clients = (await local_client.get("/dev/clients")).json()

    assert {c["source"] for c in clients.values()} <= {"measured", "typical"}
    assert clients["koreader"]["source"] == "measured"
    assert clients["android"]["source"] == "typical"
