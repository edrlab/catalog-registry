"""One e2e test per health route, over the real ASGI path."""

import pytest
from httpx import ASGITransport, AsyncClient

from registry.core.config import Settings
from registry.main import create_app

pytestmark = pytest.mark.e2e


async def test_liveness_returns_200(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_readiness_returns_200_when_the_database_is_reachable(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_every_response_carries_a_request_id(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.headers["x-request-id"]


async def test_an_incoming_request_id_is_echoed_back(client: AsyncClient) -> None:
    response = await client.get("/health/live", headers={"X-Request-Id": "abc-123"})

    assert response.headers["x-request-id"] == "abc-123"


async def test_liveness_names_the_cloud_run_revision_when_there_is_one(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("K_REVISION", "registry-00013-cnz")

    response = await client.get("/health/live")

    assert response.json() == {"status": "alive", "revision": "registry-00013-cnz"}


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
async def test_a_health_route_answers_head(client: AsyncClient, path: str) -> None:
    """Uptime checks often probe with HEAD; a 405 there reads as the service being down."""
    response = await client.head(path)

    assert response.status_code == 200
    assert response.content == b""


async def test_readiness_returns_503_when_the_database_cannot_be_reached(
    settings: Settings,
) -> None:
    """Nothing listens on port 1, so the probe's connection is refused at once."""
    unreachable = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://registry:unused@127.0.0.1:1/registry"}
    )
    built = create_app(unreachable)
    async with (
        built.router.lifespan_context(built),
        AsyncClient(transport=ASGITransport(app=built), base_url="http://testserver") as client,
    ):
        response = await client.get("/health/ready")

    assert (response.status_code, response.json()) == (503, {"status": "not ready"})
