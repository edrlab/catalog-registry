"""`SafeHttpClient`: redirects re-checked, bodies capped, one deadline, retries only when worth it.

`httpx.MockTransport` stands in for the network; the pinned connection is `test_public_backend.py`.
"""

import asyncio
import gzip
import tracemalloc
import zlib
from collections.abc import Callable, Iterator

import brotli
import httpx
import pytest

from registry.core.constants import OUTBOUND_USER_AGENT
from registry.domain.fetching import FetchError, FetchResult
from registry.network.retry import RetryPolicy
from registry.network.safe_client import SafeHttpClient

FEED = "https://feeds.example.org/opds"
ACCEPT = "application/opds+json"
Handler = Callable[[httpx.Request], httpx.Response]


class StreamingTransport(httpx.MockTransport):
    """`httpx.MockTransport` whose answers stream as a socket's do. httpx reads a `content=` or
    `json=` body (decoding it) as soon as the response is built, which no real transport does;
    such a body is handed over again as a stream. An encoded body is built with `stream=`."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        if not response.is_stream_consumed:
            return response
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=httpx.ByteStream(response.content),
        )


def encoded(body: bytes, coding: str) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Encoding": coding}, stream=httpx.ByteStream(body))


async def fetch(
    handler: Handler,
    url: str = FEED,
    *,
    max_bytes: int = 1000,
    deadline: float = 5.0,
    method: str = "GET",
    truncate: bool = False,
    policy: RetryPolicy | None = None,
    sleeps: list[float] | None = None,
) -> FetchResult:
    async def sleep(seconds: float) -> None:
        (sleeps if sleeps is not None else []).append(seconds)

    async with SafeHttpClient(
        transport=StreamingTransport(handler),
        policy=policy or RetryPolicy(random=lambda: 0.0),
        sleep=sleep,
    ) as client:
        return await client.fetch(
            url,
            accept=ACCEPT,
            max_bytes=max_bytes,
            deadline=deadline,
            method=method,
            truncate=truncate,
        )


async def refused(handler: Handler, url: str = FEED, **options: object) -> FetchError:
    with pytest.raises(FetchError) as raised:
        await fetch(handler, url, **options)  # type: ignore[arg-type]
    return raised.value


def redirect(status: int, location: str) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


async def test_it_sends_only_its_own_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    await fetch(handler)

    headers = seen[0].headers
    assert headers["user-agent"] == OUTBOUND_USER_AGENT
    assert headers["accept"] == ACCEPT
    assert headers["accept-encoding"] == "gzip"
    assert "authorization" not in headers
    assert "cookie" not in headers


async def test_cookies_a_server_sets_are_not_sent_back() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/opds":
            return httpx.Response(
                302, headers={"Location": "/next", "Set-Cookie": "session=abc; Path=/"}
            )
        return httpx.Response(200, json={})

    await fetch(handler)

    assert "cookie" not in seen[1].headers


async def test_redirects_are_followed_and_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return {
            "/opds": redirect(301, "/v2/opds"),
            "/v2/opds": redirect(308, "https://cdn.example.org/opds.json"),
        }.get(request.url.path, httpx.Response(200, json={"ok": True}))

    result = await fetch(handler)

    assert result.final_url == "https://cdn.example.org/opds.json"
    assert [(hop.url, hop.status) for hop in result.redirects] == [
        (FEED, 301),
        ("https://feeds.example.org/v2/opds", 308),
    ]


async def test_see_other_turns_any_method_into_get() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return redirect(303, "/other") if request.url.path == "/opds" else httpx.Response(200)

    await fetch(handler, method="HEAD")

    assert methods == ["HEAD", "GET"]


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/admin",
        "http://[::1]/",
        "file:///etc/passwd",
        "ftp://example.org/",
        "https://user:pw@example.org/",
        "http://2130706433/",
    ],
)
async def test_a_redirect_to_an_unsafe_address_is_refused(target: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return redirect(302, target)

    error = await refused(handler)

    assert error.code == "redirect-unsafe"
    assert calls == [FEED]


async def test_a_redirect_loop_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return redirect(302, "/b" if request.url.path == "/opds" else "/opds")

    assert (await refused(handler)).code == "redirect-loop"


async def test_more_than_five_redirects_are_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        step = int(request.url.params.get("n", "0"))
        return redirect(302, f"/opds?n={step + 1}")

    assert (await refused(handler)).code == "redirect-loop"


async def test_a_body_over_the_limit_is_too_large() -> None:
    assert (await refused(lambda _: httpx.Response(200, content=b"x" * 1001))).code == (
        "fetch-too-large"
    )


async def test_a_body_at_the_limit_is_accepted() -> None:
    result = await fetch(lambda _: httpx.Response(200, content=b"x" * 1000))

    assert len(result.body) == 1000


async def test_the_limit_applies_after_decompression() -> None:
    bomb = gzip.compress(b"\0" * 5_000_000)
    assert len(bomb) < 10_000

    assert (await refused(lambda _: encoded(bomb, "gzip"), max_bytes=1_000_000)).code == (
        "fetch-too-large"
    )


def gzip_bomb(size: int) -> bytes:
    """*size* zero bytes, gzipped, without holding *size* bytes at once."""
    packer = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    block = b"\0" * 1_000_000
    return b"".join(packer.compress(block) for _ in range(size // len(block))) + packer.flush()


async def peak_memory_of(handler: Handler, **options: object) -> tuple[FetchResult | str, int]:
    """The fetch's result (or its error code) and the most memory it held at once."""
    tracemalloc.start()
    try:
        try:
            outcome: FetchResult | str = await fetch(handler, **options)  # type: ignore[arg-type]
        except FetchError as error:
            outcome = error.code
        return outcome, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


async def test_a_gzip_bomb_is_inflated_only_up_to_the_cap() -> None:
    bomb = gzip_bomb(50_000_000)

    def handler(_: httpx.Request) -> httpx.Response:
        return encoded(bomb, "gzip")

    refused_code, peak = await peak_memory_of(handler)
    probe, probe_peak = await peak_memory_of(handler, truncate=True)

    assert refused_code == "fetch-too-large"
    assert isinstance(probe, FetchResult)
    assert probe.body == b"\0" * 1000
    assert probe.truncated is True
    assert max(peak, probe_peak) < 5_000_000


async def test_a_deflate_body_is_inflated() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return encoded(zlib.compress(b"z" * 900), "deflate")

    assert (await fetch(handler)).body == b"z" * 900


async def test_a_corrupt_gzip_body_fails() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return encoded(b"not gzip", "gzip")

    assert (await refused(handler)).code == "fetch-failed"


@pytest.mark.parametrize("coding", ["br", "zstd", "compress", "gzip, br"])
async def test_an_encoding_not_asked_for_is_refused_unread(coding: str) -> None:
    bomb = brotli.compress(b"\0" * 20_000_000, quality=1)

    def handler(_: httpx.Request) -> httpx.Response:
        return encoded(bomb, coding)

    code, peak = await peak_memory_of(handler)

    assert code == "fetch-failed"
    assert peak < 5_000_000


async def test_a_lying_content_length_does_not_raise_the_limit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2000, headers={"Content-Length": "10"})

    with pytest.raises((FetchError, httpx.HTTPError)):
        await fetch(handler)


async def test_a_probe_keeps_only_the_first_bytes() -> None:
    result = await fetch(lambda _: httpx.Response(200, content=b"y" * 5000), truncate=True)

    assert result.body == b"y" * 1000
    assert result.truncated is True


async def test_a_busy_server_is_retried_with_backoff() -> None:
    answers = iter([503, 502, 200])
    sleeps: list[float] = []

    result = await fetch(lambda _: httpx.Response(next(answers)), sleeps=sleeps)

    assert result.status == 200
    assert result.attempts == 3
    assert sleeps == [0.5, 1.0]


async def test_a_server_still_busy_after_the_retries_returns_its_status() -> None:
    sleeps: list[float] = []

    result = await fetch(lambda _: httpx.Response(503), sleeps=sleeps)

    assert result.status == 503
    assert len(sleeps) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 500])
async def test_a_client_or_server_error_is_not_retried(status: int) -> None:
    sleeps: list[float] = []

    result = await fetch(lambda _: httpx.Response(status), sleeps=sleeps)

    assert result.status == status
    assert sleeps == []


async def test_retry_after_is_honoured_when_short_and_ignored_when_long() -> None:
    short = iter([httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200)])
    sleeps: list[float] = []
    await fetch(lambda _: next(short), sleeps=sleeps)
    assert sleeps == [1.0]

    sleeps = []
    result = await fetch(
        lambda _: httpx.Response(429, headers={"Retry-After": "600"}), sleeps=sleeps
    )
    assert result.status == 429
    assert sleeps == []


async def test_a_dropped_connection_is_retried_then_reported() -> None:
    attempts: Iterator[Exception | httpx.Response] = iter(
        [httpx.ConnectError("reset"), httpx.Response(200)]
    )

    def flaky(_: httpx.Request) -> httpx.Response:
        outcome = next(attempts)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert (await fetch(flaky)).status == 200

    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert (await refused(down)).code == "fetch-failed"


async def test_no_retry_starts_past_the_deadline() -> None:
    sleeps: list[float] = []

    result = await fetch(lambda _: httpx.Response(503), deadline=0.4, sleeps=sleeps)

    assert result.status == 503
    assert sleeps == []


async def test_a_slow_server_is_cut_off_by_the_deadline() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return httpx.Response(200)

    async with SafeHttpClient(transport=StreamingTransport(slow)) as client:
        with pytest.raises(FetchError) as raised:
            await client.fetch(FEED, accept=ACCEPT, max_bytes=1000, deadline=0.2)

    assert raised.value.code == "fetch-timed-out"


async def test_an_unsafe_first_url_sends_nothing() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    assert (await refused(handler, "http://169.254.169.254/")).code == "url-unsafe"
    assert calls == []
