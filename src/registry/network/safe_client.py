"""The registry's one outbound HTTP client for URLs someone typed.

- Every URL, the first and each redirect target, passes `validate_outbound_url`; every connection
  goes through `PublicOnlyTransport`, which connects only to the public address it checked.
- Redirects are followed by hand, at most `max_redirects`, never to a URL seen before.
- The body is streamed and stops at `max_bytes` after decompression, so a gzip bomb or a lying
  `Content-Length` costs at most that much.
- One deadline covers the whole fetch, redirects and retries included, so a server sending one byte
  at a time cannot hold it past the budget.
- Transient failures are retried by `RetryPolicy`, never past the deadline.
- No cookies are kept between requests, and nothing but the fixed headers is sent.
"""

import asyncio
import dataclasses
import itertools
import time
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self
from urllib.parse import urljoin

import httpx

from registry.core.constants import OUTBOUND_USER_AGENT
from registry.domain.fetching import FetchError, FetchResult, Hop
from registry.domain.url_policy import validate_outbound_url
from registry.network.public_backend import PublicOnlyTransport
from registry.network.retry import RETRY_STATUSES, RetryPolicy

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DEFAULT_MAX_REDIRECTS = 5
#: Per operation. The caller's deadline bounds the whole fetch; a slow library server may take
#: 10 s before its first byte.
TIMEOUTS = httpx.Timeout(connect=4.0, read=10.0, write=4.0, pool=2.0)

#: Failures another attempt may cure. A refused URL (`FetchError`) is never among them.
_TRANSIENT = (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError)


@dataclasses.dataclass(frozen=True, slots=True)
class _Request:
    method: str
    accept: str
    max_bytes: int
    truncate: bool
    #: `time.monotonic()` value past which no retry starts.
    ends_at: float


class SafeHttpClient:
    """`async with SafeHttpClient() as client: await client.fetch(url, ...)`."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        policy: RetryPolicy | None = None,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._policy = policy or RetryPolicy()
        self._max_redirects = max_redirects
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            transport=self._transport or PublicOnlyTransport(),
            follow_redirects=False,
            timeout=TIMEOUTS,
            headers={"User-Agent": OUTBOUND_USER_AGENT},
            trust_env=False,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(
        self,
        url: str,
        *,
        accept: str,
        max_bytes: int,
        deadline: float,
        method: str = "GET",
        truncate: bool = False,
    ) -> FetchResult:
        """Request *url* within *deadline* seconds. With *truncate*, a body over *max_bytes* is
        cut there (a probe); without, it is `fetch-too-large`. Raises `FetchError`."""
        started = time.monotonic()
        try:
            async with asyncio.timeout(deadline):
                return await self._follow(
                    url, _Request(method, accept, max_bytes, truncate, started + deadline)
                )
        except TimeoutError:
            raise FetchError(
                "fetch-timed-out", f"No complete answer within {deadline:g} seconds.", url=url
            ) from None

    async def _follow(self, url: str, request: "_Request") -> FetchResult:
        started = time.monotonic()
        current, seen, attempts = url, {url}, 0
        hops: list[Hop] = []
        validate_outbound_url(current)
        while True:
            response, body, cut, tries = await self._send_with_retries(current, request)
            attempts += tries
            location = response.headers.get("location")
            if response.status_code not in REDIRECT_STATUSES or not location:
                return FetchResult(
                    url=url,
                    final_url=current,
                    status=response.status_code,
                    headers={name.lower(): value for name, value in response.headers.items()},
                    body=body,
                    redirects=tuple(hops),
                    truncated=cut,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    attempts=attempts,
                )
            hops.append(Hop(url=current, status=response.status_code))
            target = urljoin(current, location)
            if len(hops) > self._max_redirects or target in seen:
                raise FetchError(
                    "redirect-loop",
                    f"More than {self._max_redirects} redirects, or a redirect back to an "
                    "address already visited.",
                    url=url,
                    redirects=tuple(hops),
                )
            try:
                validate_outbound_url(target)
            except FetchError as refused:
                raise FetchError(
                    "redirect-unsafe", refused.detail, url=url, redirects=tuple(hops)
                ) from None
            seen.add(target)
            current = target
            if response.status_code == 303:  # noqa: PLR2004 - RFC 9110 §15.4.4: See Other is a GET
                request = dataclasses.replace(request, method="GET")

    async def _send_with_retries(
        self, url: str, request: "_Request"
    ) -> tuple[httpx.Response, bytes, bool, int]:
        """One hop, retried while the failure is transient and the deadline allows. Returns the
        response, its body, whether the body was cut, and the attempts made."""
        for attempt in itertools.count(1):
            try:
                response, body, cut = await self._send(url, request)
            except _TRANSIENT as error:
                delay = self._policy.build_delay(attempt)
                if delay is None or time.monotonic() + delay >= request.ends_at:
                    raise _transport_failure(error, url) from None
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response, body, cut, attempt
                delay = self._policy.build_delay(attempt, response.headers.get("retry-after"))
                if delay is None or time.monotonic() + delay >= request.ends_at:
                    return response, body, cut, attempt
            await self._sleep(delay)
        raise AssertionError("unreachable")

    async def _send(self, url: str, request: "_Request") -> tuple[httpx.Response, bytes, bool]:
        assert self._client is not None, "use `async with SafeHttpClient()`"
        chunks: list[bytes] = []
        size = 0
        cut = False
        max_bytes = request.max_bytes
        async with self._client.stream(
            request.method, url, headers={"Accept": request.accept}
        ) as response:
            if request.method != "HEAD":
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        if not request.truncate:
                            raise FetchError(
                                "fetch-too-large",
                                f"The answer is larger than {max_bytes} bytes.",
                                url=url,
                            )
                        chunks.append(chunk[: max_bytes - (size - len(chunk))])
                        cut = True
                        break
                    chunks.append(chunk)
        # Nothing carries over to the next request: not cookies, not anything a server set.
        self._client.cookies.clear()
        return response, b"".join(chunks), cut


def _transport_failure(error: Exception, url: str) -> FetchError:
    if isinstance(error, httpx.TimeoutException):
        return FetchError("fetch-timed-out", "The server did not answer in time.", url=url)
    return FetchError("fetch-failed", "The connection to the server failed.", url=url)
