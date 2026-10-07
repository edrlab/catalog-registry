"""The middleware on a minimal ASGI app, so each HTTP method can be driven exactly (ADR-061).

HEAD is the case the application's own routes never exercise (they answer GET only), but a response
to HEAD stands for the GET: its `Content-Length` is the size of the body it does not send.
"""

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
