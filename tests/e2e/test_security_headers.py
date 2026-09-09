"""Headers and error shape on the public surface. the conventions."""

import pytest
from httpx import ASGITransport, AsyncClient

from registry.api.middleware import SECURITY_HEADERS

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("path", ["/", "/health/live", "/health/ready"])
async def test_security_headers_are_on_every_response(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


async def test_security_headers_are_on_error_responses_too(client: AsyncClient) -> None:
    """The 404 path renders through the exception handlers, which bypass the router."""
    response = await client.get("/catalogs/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_the_feed_is_readable_cross_origin(client: AsyncClient) -> None:
    """Public data, and browser-based readers are a legitimate consumer."""
    response = await client.get("/", headers={"Origin": "https://reader.example"})

    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


async def test_writes_are_not_advertised_cross_origin(client: AsyncClient) -> None:
    """Nothing writable exists yet, and the back office (v1.0) must not inherit this."""
    response = await client.options(
        "/",
        headers={"Origin": "https://reader.example", "Access-Control-Request-Method": "POST"},
    )

    assert "POST" not in response.headers.get("access-control-allow-methods", "")


async def test_an_unexpected_error_is_a_problem_document_and_leaks_nothing(app) -> None:
    """The detail must never be `str(exc)`, this is a public, unauthenticated endpoint."""
    secret = "postgresql://user:hunter2@db/registry"

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(secret)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as raw:
        response = await raw.get("/boom")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    # Starlette sends a 500 past every middleware the application adds, so the headers have
    # to come from the problem response itself.
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert secret not in response.text
    assert "hunter2" not in response.text
    assert response.json()["title"] == "Internal Server Error"
