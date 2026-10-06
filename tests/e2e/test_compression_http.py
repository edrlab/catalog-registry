"""Compression over the real ASGI path: what each kind of client actually gets (ADR-061).

The clients are the ones measured in docs/search.md: Thorium Reader and browsers offer `br`, most
libraries only `gzip`, KOReader and Python's urllib `identity` (the body must stay as it is), and
curl with no flags nothing.
"""

import gzip
import json
import logging
from collections.abc import Iterator

import brotli
import pytest
from httpx import AsyncClient

from registry.api.compression import MINIMUM_SIZE

pytestmark = pytest.mark.e2e

PLAIN = {"Accept-Encoding": "identity"}


async def plain_body(client: AsyncClient, path: str) -> bytes:
    response = await client.get(path, headers=PLAIN)
    assert "content-encoding" not in response.headers
    return response.content


# `httpx` decodes gzip and brotli for us, which would hide the wire format: read the raw bytes.
async def raw(
    client: AsyncClient, path: str, accept: str | None
) -> tuple[int, dict[str, str], bytes]:
    request = client.build_request("GET", path)
    request.headers.pop("accept-encoding", None)
    if accept is not None:
        request.headers["accept-encoding"] = accept
    response = await client.send(request, stream=True)
    try:
        body = b"".join([chunk async for chunk in response.aiter_raw()])
    finally:
        await response.aclose()
    return response.status_code, dict(response.headers), body


async def test_a_client_that_offers_brotli_gets_brotli(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    expected = await plain_body(client, "/")

    status, headers, body = await raw(client, "/", "gzip, deflate, br")

    assert status == 200
    assert headers["content-encoding"] == "br"
    assert brotli.decompress(body) == expected
    assert len(body) < len(expected) / 3
    assert int(headers["content-length"]) == len(body)


async def test_a_client_that_offers_only_gzip_gets_gzip(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    expected = await plain_body(client, "/")

    _, headers, body = await raw(client, "/", "gzip, deflate")

    assert headers["content-encoding"] == "gzip"
    assert gzip.decompress(body) == expected
    assert int(headers["content-length"]) == len(body)


@pytest.mark.parametrize("offered", ["identity", None, "", "deflate, zstd", "br;q=0, gzip;q=0"])
async def test_a_client_that_refuses_or_names_nothing_we_offer_gets_the_body_as_it_is(
    client: AsyncClient, seeded_catalogs: int, offered: str | None
) -> None:
    """KOReader sends `identity`; a plain curl sends nothing; neither can decode anything."""
    expected = await plain_body(client, "/")

    _, headers, body = await raw(client, "/", offered)

    assert "content-encoding" not in headers
    assert body == expected
    assert json.loads(body)["metadata"]["title"]


async def test_the_clients_preference_outranks_the_servers(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    _, headers, _ = await raw(client, "/", "gzip;q=1.0, br;q=0.4")

    assert headers["content-encoding"] == "gzip"


async def test_a_refused_coding_is_not_used(client: AsyncClient, seeded_catalogs: int) -> None:
    _, headers, _ = await raw(client, "/", "br;q=0, gzip")

    assert headers["content-encoding"] == "gzip"


@pytest.mark.parametrize("offered", ["gzip, br", "identity", None])
async def test_every_answer_says_it_varies_by_accept_encoding(
    client: AsyncClient, seeded_catalogs: int, offered: str | None
) -> None:
    """Compressed or not, a cache must not hand this answer to a client that asked differently."""
    _, headers, _ = await raw(client, "/", offered)

    assert "accept-encoding" in headers["vary"].lower()


async def test_a_small_response_is_not_compressed_even_when_asked(client: AsyncClient) -> None:
    status, headers, body = await raw(client, "/health/live", "gzip, br")

    assert status == 200
    assert len(body) < MINIMUM_SIZE
    assert "content-encoding" not in headers
    assert int(headers["content-length"]) == len(body)
    assert "accept-encoding" in headers["vary"].lower()


async def test_an_error_response_is_still_a_problem_document(client: AsyncClient) -> None:
    status, headers, body = await raw(client, "/search?page=0", "gzip, br")

    assert status == 422
    assert json.loads(brotli.decompress(body) if headers.get("content-encoding") == "br" else body)


async def test_gzip_is_the_same_bytes_every_time(client: AsyncClient, seeded_catalogs: int) -> None:
    """No timestamp in the gzip header, so a cache or a test can compare responses byte for byte."""
    _, _, first = await raw(client, "/", "gzip")
    _, _, second = await raw(client, "/", "gzip")

    assert first == second


async def test_security_and_request_id_headers_survive_compression(
    client: AsyncClient, seeded_catalogs: int
) -> None:
    _, headers, _ = await raw(client, "/", "br")

    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-request-id"]
    assert headers["content-type"].startswith("application/opds-catalog+json")


async def test_a_search_page_is_compressed_too(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    expected = (await client.get("/search?query=bibliotheque", headers=PLAIN)).content
    assert len(expected) >= MINIMUM_SIZE, "the page must be big enough to be worth compressing"

    _, headers, body = await raw(client, "/search?query=bibliotheque", "gzip, deflate, br")

    assert headers["content-encoding"] == "br"
    assert brotli.decompress(body) == expected


# --- who asked: one JSON line per request, and nothing else about them ---------------------------


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
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


async def test_each_request_logs_the_encoding_offered_and_chosen_and_the_user_agent(
    client: AsyncClient, seeded_catalogs: int, client_log: Lines
) -> None:
    await client.get(
        "/", headers={"Accept-Encoding": "gzip, deflate, br", "User-Agent": "Thorium/3.5 test"}
    )

    (line,) = client_log.lines
    assert line == {
        "severity": "INFO",
        "message": "client",
        "path": "/",
        "status": 200,
        "accept_encoding": "gzip, deflate, br",
        "encoding": "br",
        "user_agent": "Thorium/3.5 test",
    }


async def test_a_client_that_asked_for_nothing_is_logged_as_such(
    client: AsyncClient, seeded_catalogs: int, client_log: Lines
) -> None:
    await client.get("/", headers={"Accept-Encoding": "identity", "User-Agent": "KOReader"})

    (line,) = client_log.lines
    assert line["accept_encoding"] == "identity"
    assert line["encoding"] is None


async def test_the_log_holds_no_query_string_address_or_other_header(
    client: AsyncClient, searchable_catalogs: None, client_log: Lines
) -> None:
    await client.get(
        "/search?query=secret+reading+list",
        headers={"Authorization": "Bearer x", "Cookie": "a=b", "X-Forwarded-For": "203.0.113.9"},
    )

    (line,) = client_log.lines
    text = json.dumps(line)
    assert "secret" not in text
    assert "203.0.113.9" not in text
    assert "Bearer" not in text
    assert line["path"] == "/search"
    assert set(line) == {
        "severity",
        "message",
        "path",
        "status",
        "accept_encoding",
        "encoding",
        "user_agent",
    }


async def test_a_very_long_user_agent_is_cut(
    client: AsyncClient, seeded_catalogs: int, client_log: Lines
) -> None:
    await client.get("/", headers={"User-Agent": "A" * 5000})

    (line,) = client_log.lines
    assert len(str(line["user_agent"])) == 200
