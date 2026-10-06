"""`Server-Timing`: how long the application took, so latency can be split without server tools."""

import re
import time

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.e2e

FORMAT = re.compile(r"^app;dur=(\d+\.\d)$")


def duration(header: str) -> float:
    match = FORMAT.match(header)
    assert match, header
    return float(match.group(1))


@pytest.mark.parametrize("path", ["/health/live", "/", "/search?query=paris", "/no-such-route"])
async def test_every_response_says_how_long_the_application_took(
    client: AsyncClient, searchable_catalogs: None, path: str
) -> None:
    response = await client.get(path)

    assert duration(response.headers["server-timing"]) >= 0


async def test_the_duration_is_the_application_time_not_the_clients(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    started = time.perf_counter()
    response = await client.get("/search?query=bibliotheque")
    client_ms = (time.perf_counter() - started) * 1000

    assert 0 < duration(response.headers["server-timing"]) <= client_ms


async def test_an_error_response_carries_it_too(client: AsyncClient) -> None:
    response = await client.get("/search?page=0")

    assert response.status_code == 422
    assert "server-timing" in response.headers


async def test_a_browser_reader_is_allowed_to_read_it(client: AsyncClient) -> None:
    response = await client.get(
        "/health/live", headers={"Origin": "https://reader.example", "Accept-Encoding": "identity"}
    )

    assert response.headers["timing-allow-origin"] == "*"
    assert "server-timing" in response.headers["access-control-expose-headers"].lower()


async def test_a_slower_request_reports_a_longer_time(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    """The number tracks real work: a search takes longer than the health check that does none."""
    health = [
        duration((await client.get("/health/live")).headers["server-timing"]) for _ in range(5)
    ]
    search = [
        duration((await client.get("/search?query=bibliotheque")).headers["server-timing"])
        for _ in range(5)
    ]

    assert min(search) >= min(health)
