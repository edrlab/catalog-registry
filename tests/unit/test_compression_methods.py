"""The middleware on a minimal ASGI app, so each HTTP method can be driven exactly (ADR-061).

HEAD is the case the application's own routes never exercise (they answer GET only), but a response
to HEAD stands for the GET: its `Content-Length` is the size of the body it does not send.
"""

import json
import logging
from collections.abc import Iterator

import brotli
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from registry.api.compression import MINIMUM_SIZE, CompressionMiddleware

pytestmark = pytest.mark.unit

BODY = b'{"catalogs": [' + b'"x",' * 2000 + b'"y"]}'


async def document(request: Request) -> Response:
    if request.method == "HEAD":
        # What a framework does for HEAD: the headers of the GET, an empty body.
        return Response(
            b"", media_type="application/json", headers={"Content-Length": str(len(BODY))}
        )
    return Response(BODY, media_type="application/json")


def client() -> AsyncClient:
    app = Starlette(routes=[Route("/doc", document, methods=["GET", "HEAD", "POST"])])
    return AsyncClient(
        transport=ASGITransport(app=CompressionMiddleware(app)), base_url="http://test"
    )


async def test_a_head_response_keeps_the_content_length_of_the_get_it_stands_for() -> None:
    async with client() as http:
        response = await http.head("/doc", headers={"Accept-Encoding": "gzip, br"})

    assert len(BODY) > MINIMUM_SIZE
    assert response.headers["content-length"] == str(len(BODY))
    assert response.content == b""


async def test_a_head_response_is_not_marked_compressed_when_no_body_was_compressed() -> None:
    async with client() as http:
        response = await http.head("/doc", headers={"Accept-Encoding": "br"})

    assert "content-encoding" not in response.headers


@pytest.mark.parametrize("offered", ["gzip, br", "identity", None])
async def test_a_head_response_still_says_it_varies_by_accept_encoding(
    offered: str | None,
) -> None:
    headers = {} if offered is None else {"Accept-Encoding": offered}
    async with client() as http:
        response = await http.head("/doc", headers=headers)

    assert "accept-encoding" in response.headers["vary"].lower()


async def test_the_get_for_the_same_route_is_still_compressed() -> None:
    async with client() as http:
        request = http.build_request("GET", "/doc", headers={"Accept-Encoding": "br"})
        response = await http.send(request, stream=True)
        raw = b"".join([chunk async for chunk in response.aiter_raw()])
        await response.aclose()

    assert response.headers["content-encoding"] == "br"
    assert brotli.decompress(raw) == BODY
    assert response.headers["content-length"] == str(len(raw))


async def test_a_post_response_is_compressed_like_a_get() -> None:
    async with client() as http:
        request = http.build_request("POST", "/doc", headers={"Accept-Encoding": "br"})
        response = await http.send(request, stream=True)
        raw = b"".join([chunk async for chunk in response.aiter_raw()])
        await response.aclose()

    assert response.headers["content-encoding"] == "br"
    assert brotli.decompress(raw) == BODY


# --- the one log line per request, on every path, and 406 ---------------------------------------


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(record.getMessage()))


@pytest.fixture
def client_log() -> Iterator[Lines]:
    logger = logging.getLogger("registry.clients")
    handler = Lines()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


async def test_a_head_request_is_logged_once(client_log: Lines) -> None:
    async with client() as http:
        await http.head("/doc", headers={"Accept-Encoding": "gzip", "User-Agent": "probe/1"})

    assert [(line["path"], line["status"], line["user_agent"]) for line in client_log.lines] == [
        ("/doc", 200, "probe/1")
    ]


async def test_a_request_that_crashes_the_app_is_logged_as_a_500_and_the_error_goes_on(
    client_log: Lines,
) -> None:
    async def boom(request: Request) -> Response:
        raise RuntimeError("unexpected")

    app = Starlette(routes=[Route("/boom", boom)])
    transport = ASGITransport(app=CompressionMiddleware(app), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/boom", headers={"Accept-Encoding": "br"})

    assert response.status_code == 500  # Starlette's own error middleware answered
    assert [(line["path"], line["status"]) for line in client_log.lines] == [("/boom", 500)]


async def test_a_normal_request_is_logged_exactly_once(client_log: Lines) -> None:
    async with client() as http:
        await http.get("/doc", headers={"Accept-Encoding": "gzip"})

    assert len(client_log.lines) == 1
    assert client_log.lines[0]["encoding"] == "gzip"


@pytest.mark.parametrize("offered", ["identity;q=0", "*;q=0", "identity;q=0, deflate"])
async def test_a_client_that_refuses_every_coding_and_identity_gets_406(
    client_log: Lines, offered: str
) -> None:
    async with client() as http:
        response = await http.get("/doc", headers={"Accept-Encoding": offered})

    assert response.status_code == 406
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 406
    assert "accept-encoding" in response.headers["vary"].lower()
    assert response.headers["x-content-type-options"] == "nosniff"
    assert [(line["status"], line["encoding"]) for line in client_log.lines] == [(406, None)]


@pytest.mark.parametrize("offered", ["identity;q=0, gzip", "*;q=0, gzip", "identity;q=0, br"])
async def test_refusing_identity_is_fine_when_a_coding_we_offer_is_accepted(offered: str) -> None:
    async with client() as http:
        response = await http.get("/doc", headers={"Accept-Encoding": offered})

    assert response.status_code == 200
    assert response.headers["content-encoding"] in {"gzip", "br"}
