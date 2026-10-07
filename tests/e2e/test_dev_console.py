"""The dev console exists on a developer's machine and nowhere else (ADR-063)."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.api.routes import dev
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
@pytest.mark.parametrize(
    "path",
    [
        "/dev",
        "/dev/clients",
        "/dev/simulate?path=/&client=curl",
        "/dev/fetch?url=https://example.org/",
    ],
)
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
        ("browser", "zstd"),
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


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/feed",
        "file:///etc/passwd",
        "http://localhost/feed",
        "http://127.0.0.1:8000/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
    ],
)
async def test_fetch_refuses_anything_but_a_public_host(
    local_client: AsyncClient, url: str
) -> None:
    response = await local_client.get("/dev/fetch", params={"url": url})

    assert response.status_code == 422


def _public(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allowed(url: str) -> None:
        return None

    monkeypatch.setattr(dev, "require_public_host", allowed)


async def test_fetch_returns_the_document_and_the_wire_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public(monkeypatch)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"metadata": {"title": "A library"}})
    )

    status, _, body, wire = await dev.fetch_public(
        "https://lib.example/feed", {}, transport=transport
    )

    assert status == 200
    assert b"A library" in body
    assert wire == 0  # the mock transport has no wire


async def test_fetch_checks_every_redirect_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[str] = []

    async def record(url: str) -> None:
        checked.append(url)

    monkeypatch.setattr(dev, "require_public_host", record)
    transport = httpx.MockTransport(
        lambda request: (
            httpx.Response(302, headers={"location": "https://other.example/feed"})
            if request.url.host == "lib.example"
            else httpx.Response(200, json={})
        )
    )

    await dev.fetch_public("https://lib.example/feed", {}, transport=transport)

    assert checked == ["https://lib.example/feed", "https://other.example/feed"]


async def test_a_redirect_to_a_private_address_is_refused() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    )

    with pytest.raises(HTTPException) as raised:
        await dev.fetch_public("http://93.184.216.34/feed", {}, transport=transport)

    assert raised.value.status_code == 422


async def test_a_redirect_loop_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    _public(monkeypatch)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://lib.example/again"})
    )

    with pytest.raises(HTTPException, match="too many redirects"):
        await dev.fetch_public("https://lib.example/feed", {}, transport=transport)


async def test_an_oversized_feed_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _public(monkeypatch)
    monkeypatch.setattr(dev, "FETCH_MAX_BYTES", 100)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 500))

    with pytest.raises(HTTPException, match="over 2 MB"):
        await dev.fetch_public("https://lib.example/feed", {}, transport=transport)
