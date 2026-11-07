"""The dev console, and which parts of it exist where."""

import asyncio
import gzip
import json
import logging
import socket
import zlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import brotli
import httpx
import pytest
import zstandard
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Message, Receive, Scope, Send

from registry.api.routes import dev, dev_fetch
from registry.core.config import Settings
from registry.domain.fetching import FetchError
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


ENVIRONMENTS = ["local", "test", "staging", "production"]


@pytest.fixture
def console_in(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> "Callable[[str], AbstractAsyncContextManager[AsyncClient]]":
    """The real app for one environment, with the test database behind it."""

    @asynccontextmanager
    async def build(environment: str) -> AsyncIterator[AsyncClient]:
        built = create_app(settings.model_copy(update={"environment": environment}))
        async with built.router.lifespan_context(built):
            built.state.session_factory = session_factory
            built.state.search_session_factory = session_factory
            async with AsyncClient(
                transport=ASGITransport(app=built), base_url="http://testserver"
            ) as client:
                yield client

    return build


@pytest.mark.parametrize("environment", ENVIRONMENTS)
async def test_the_page_and_the_simulator_exist_in_every_environment(
    console_in: "Callable[[str], AbstractAsyncContextManager[AsyncClient]]",
    searchable_catalogs: None,
    environment: str,
) -> None:
    """Production runs `staging`. The console reads only public data, so it is mounted in all."""
    async with console_in(environment) as client:
        page = await client.get("/dev")
        clients = await client.get("/dev/clients")
        simulated = await client.get(
            SIMULATE, params={"path": "/search?query=paris", "client": "thorium"}
        )

    assert page.status_code == 200
    assert clients.status_code == 200
    assert simulated.status_code == 200
    assert simulated.json()["body"]["catalogs"], "it really searched"


@pytest.mark.parametrize("environment", ["test", "staging", "production"])
async def test_reading_a_library_feed_exists_only_in_a_local_run(
    settings: Settings, environment: str
) -> None:
    """`/dev/fetch` makes the server request a URL a visitor names: never in a deployed service."""
    built = create_app(settings.model_copy(update={"environment": environment}))
    async with AsyncClient(
        transport=ASGITransport(app=built), base_url="http://testserver"
    ) as client:
        response = await client.get("/dev/fetch", params={"url": "https://example.org/"})

    assert response.status_code == 404


async def test_the_console_is_not_counted_as_a_reader(
    console_in: "Callable[[str], AbstractAsyncContextManager[AsyncClient]]",
    searchable_catalogs: None,
) -> None:
    """One line per request feeds the count of real clients. The console's own page and the requests
    it makes for a visitor must not be in it, and a real request must."""
    logger = logging.getLogger("registry.clients")
    lines: list[dict[str, object]] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(json.loads(record.getMessage()))

    handler = Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        async with console_in("staging") as client:
            await client.get("/dev")
            await client.get("/dev/clients")
            await client.get(SIMULATE, params={"path": "/search?query=paris", "client": "koreader"})
            await client.get("/", headers={"User-Agent": "a-real-reader/1"})
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert [(line["path"], line["user_agent"]) for line in lines] == [("/", "a-real-reader/1")]


@pytest.mark.parametrize(
    "language", ["fr", "fr-FR,fr;q=0.9,en;q=0.5", "en-US,en;q=0.9", "*", "de,fr;q=0.5"]
)
async def test_an_accept_language_value_is_accepted(
    local_client: AsyncClient, language: str
) -> None:
    response = await local_client.get(
        SIMULATE, params={"path": "/", "client": "curl", "language": language}
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "language", ["fr\r\nX-Injected: 1", "fr\nx", "<script>", "fr\x00", "fr;q=0.9\t", "é"]
)
async def test_anything_that_is_not_an_accept_language_value_is_refused(
    local_client: AsyncClient, language: str
) -> None:
    """The value becomes a request header: only the characters it is made of get through."""
    response = await local_client.get(
        SIMULATE, params={"path": "/", "client": "curl", "language": language}
    )

    assert response.status_code == 422


async def test_the_page_is_served_locally_with_a_closed_policy(local_client: AsyncClient) -> None:
    response = await local_client.get("/dev")

    assert response.status_code == 200
    assert "<title>Catalog Registry</title>" in response.text
    assert "LOCAL ONLY" not in response.text
    assert 'id="sq"' in response.text, "the Search view is part of the page"
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
    [
        "/dev",
        "/health/live",
        "http://evil.example/",
        "//evil.example/",
        "/docs",
        "/search/../dev",
        "/?restrict=opds&next=/dev",
        "/?x=1",
    ],
)
async def test_only_the_public_reads_can_be_simulated(local_client: AsyncClient, path: str) -> None:
    response = await local_client.get(SIMULATE, params={"path": path, "client": "curl"})

    assert response.status_code == 422


async def test_the_feed_can_be_simulated_with_a_restriction(
    local_client: AsyncClient, searchable_catalogs: None
) -> None:
    response = await local_client.get(
        SIMULATE,
        params={"path": "/?restrict=opds", "client": "browser", "language": "*"},
    )

    assert response.status_code == 200
    titles = [c["metadata"]["title"] for c in response.json()["body"]["catalogs"]]
    assert titles == ["Project Gutenberg"]


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

    monkeypatch.setattr(dev_fetch, "require_public_host", allowed)


async def test_fetch_returns_the_document_and_the_wire_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public(monkeypatch)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"metadata": {"title": "A library"}})
    )

    status, _, body, wire = await dev_fetch.fetch_public(
        "https://lib.example/feed", {}, transport=transport
    )

    assert status == 200
    assert b"A library" in body
    assert wire == 0  # the mock transport has no wire


async def test_fetch_checks_every_redirect_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[str] = []

    async def record(url: str) -> None:
        checked.append(url)

    monkeypatch.setattr(dev_fetch, "require_public_host", record)
    transport = httpx.MockTransport(
        lambda request: (
            httpx.Response(302, headers={"location": "https://other.example/feed"})
            if request.url.host == "lib.example"
            else httpx.Response(200, json={})
        )
    )

    await dev_fetch.fetch_public("https://lib.example/feed", {}, transport=transport)

    assert checked == ["https://lib.example/feed", "https://other.example/feed"]


async def test_a_redirect_to_a_private_address_is_refused() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    )

    with pytest.raises(HTTPException) as raised:
        await dev_fetch.fetch_public("http://93.184.216.34/feed", {}, transport=transport)

    assert raised.value.status_code == 422


async def test_a_redirect_loop_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    _public(monkeypatch)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://lib.example/again"})
    )

    with pytest.raises(HTTPException, match="too many redirects"):
        await dev_fetch.fetch_public("https://lib.example/feed", {}, transport=transport)


async def test_an_oversized_feed_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _public(monkeypatch)
    monkeypatch.setattr(dev_fetch, "FETCH_MAX_BYTES", 100)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 500))

    with pytest.raises(HTTPException, match="over 2 MB"):
        await dev_fetch.fetch_public("https://lib.example/feed", {}, transport=transport)


def _page_app(settings: Settings, environment: str) -> FastAPI:
    app = FastAPI()
    app.include_router(dev.router)
    app.state.settings = settings.model_copy(update={"environment": environment})
    return app


@pytest.mark.parametrize(
    ("environment", "fetch"),
    [("local", "true"), ("test", "false"), ("staging", "false"), ("production", "false")],
)
async def test_the_page_is_told_whether_it_can_read_a_library_feed(
    settings: Settings, environment: str, fetch: str
) -> None:
    """The buttons follow this. Only a local console has /dev/fetch, so everywhere else a library
    link must open in a new tab instead of promising a view the console cannot give."""
    transport = ASGITransport(app=_page_app(settings, environment))
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/dev")

    assert response.status_code == 200
    assert f'"environment": "{environment}"' in response.text
    assert f'"fetch": {fetch}' in response.text
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert '{"environment":"local","fetch":true}' not in response.text, "the default was replaced"


async def test_the_whole_fetch_has_one_time_budget_not_one_per_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx's timeout limits each read. A server that sends a byte every so often never trips it,
    so the fetch as a whole must run out of time."""

    async def public(url: str) -> None:
        return None

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                await asyncio.sleep(0.05)
                yield b"x"

    monkeypatch.setattr(dev_fetch, "require_public_host", public)
    monkeypatch.setattr(dev_fetch, "FETCH_TIMEOUT_SECONDS", 0.2)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Trickle()))

    with pytest.raises(HTTPException) as raised:
        await dev_fetch.fetch_public("https://slow.example/feed", {}, transport=transport)

    assert raised.value.status_code == 502
    assert "took longer than" in raised.value.detail


def test_the_fetch_sends_the_selected_clients_headers_exactly() -> None:
    thorium = dev_fetch.build_fetch_headers(dev.CLIENTS["thorium"], "fr")
    assert thorium["Accept-Encoding"] == "gzip, deflate, br"
    assert thorium["User-Agent"].startswith("Thorium")
    assert thorium["Accept-Language"] == "fr"
    assert "opds+json" in thorium["Accept"]

    koreader = dev_fetch.build_fetch_headers(dev.CLIENTS["koreader"], "")
    assert koreader["Accept-Encoding"] == "identity"
    assert "Accept-Language" not in koreader


async def test_a_profile_without_accept_encoding_goes_out_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx would add its own, and the simulated curl or .NET client would really be httpx."""

    async def public(url: str) -> None:
        return None

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(dev_fetch, "require_public_host", public)
    transport = httpx.MockTransport(handler)

    bare = dev_fetch.build_fetch_headers(dev.CLIENTS["curl"], "")
    await dev_fetch.fetch_public("https://lib.example/a", bare, transport=transport)
    named = dev_fetch.build_fetch_headers(dev.CLIENTS["thorium"], "")
    await dev_fetch.fetch_public("https://lib.example/b", named, transport=transport)

    assert "accept-encoding" not in seen[0].headers
    assert seen[0].headers["user-agent"] == dev.CLIENTS["curl"]["headers"]["User-Agent"]
    assert seen[1].headers["accept-encoding"] == "gzip, deflate, br"


# --- the simulator's in-process call --------------------------------------------------------------


async def test_a_simulated_request_carries_an_empty_body() -> None:
    """The simulator makes a GET: the app reads one empty body and no more."""
    received: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        received.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    await dev.call_app(app, "/", {})

    assert received == [{"type": "http.request", "body": b"", "more_body": False}]


async def test_trailers_do_not_become_response_headers() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [], "trailers": True})
        await send({"type": "http.response.body", "body": b"{}"})
        await send({"type": "http.response.trailers", "headers": [(b"x-checksum", b"abc")]})

    status, headers, body = await dev.call_app(app, "/", {})

    assert (status, headers, body) == (200, {}, b"{}")


@pytest.mark.parametrize(
    ("encoding", "encode"),
    [
        ("br", brotli.compress),
        ("zstd", zstandard.ZstdCompressor().compress),
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
        (None, bytes),
    ],
)
def test_every_coding_a_client_can_receive_is_decoded_for_display(
    encoding: str | None, encode: Callable[[bytes], bytes]
) -> None:
    assert dev.decode(encode(b'{"ok": true}'), encoding) == b'{"ok": true}'


async def test_a_simulated_answer_that_is_not_json_is_shown_as_text(settings: Settings) -> None:
    app = _page_app(settings, "test")

    @app.get("/")
    async def plain() -> PlainTextResponse:
        return PlainTextResponse("not a feed")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        response = await http.get(SIMULATE, params={"path": "/", "client": "curl"})

    assert (response.json()["is_json"], response.json()["body"]) == (False, "not a feed")


# --- /dev/fetch: failures on the way, and what comes back -----------------------------------------


async def test_a_name_that_does_not_resolve_is_a_bad_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unresolvable(*_args: object, **_kwargs: object) -> list[object]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", unresolvable)

    with pytest.raises(HTTPException) as raised:
        await dev_fetch.require_public_host("https://nowhere.invalid/feed")

    assert raised.value.status_code == 502


@pytest.mark.parametrize(("code", "status"), [("host-unresolvable", 502), ("url-unsafe", 422)])
async def test_a_refusal_by_the_connection_guard_keeps_its_meaning(
    monkeypatch: pytest.MonkeyPatch, code: str, status: int
) -> None:
    """A name that does not resolve is the library's server failing (502); an address the guard
    will not connect to is a request that cannot be made (422)."""
    _public(monkeypatch)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise FetchError(code, "refused by the guard")

    with pytest.raises(HTTPException) as raised:
        await dev_fetch.fetch_public(
            "https://lib.example/feed", {}, transport=httpx.MockTransport(refuse)
        )

    assert (raised.value.status_code, raised.value.detail) == (status, "refused by the guard")


async def test_a_connection_failure_is_a_bad_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    _public(monkeypatch)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(HTTPException) as raised:
        await dev_fetch.fetch_public(
            "https://lib.example/feed", {}, transport=httpx.MockTransport(unreachable)
        )

    assert raised.value.status_code == 502


def _fetch_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.include_router(dev_fetch.router)
    app.state.settings = settings.model_copy(update={"environment": "local"})
    return app


async def test_fetch_refuses_an_unknown_client(settings: Settings) -> None:
    transport = ASGITransport(app=_fetch_app(settings))
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get(
            "/dev/fetch", params={"url": "https://lib.example/feed", "client": "nobody"}
        )

    assert response.status_code == 422


def _answer(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    async def fetched(
        url: str, headers: object, *, allowed_hosts: object = None
    ) -> tuple[int, dict[str, str], bytes, int]:
        return 200, {"Content-Type": "application/opds+json"}, body, len(body)

    monkeypatch.setattr(dev_fetch, "fetch_public", fetched)


async def test_fetch_returns_a_json_feed_parsed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(monkeypatch, b'{"metadata": {"title": "A library"}}')
    transport = ASGITransport(app=_fetch_app(settings))
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/dev/fetch", params={"url": "https://lib.example/feed"})

    assert response.json()["body"] == {"metadata": {"title": "A library"}}


async def test_fetch_returns_a_page_that_is_not_json_as_text_cut_at_20000_characters(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(monkeypatch, b"<p>" + b"x" * 30_000)
    transport = ASGITransport(app=_fetch_app(settings))
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/dev/fetch", params={"url": "https://lib.example/feed"})

    shown = response.json()
    assert (shown["is_json"], len(shown["body"])) == (False, 20_000)
