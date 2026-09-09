"""One e2e test per health route, over the real ASGI path."""

import pytest
from httpx import AsyncClient

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
