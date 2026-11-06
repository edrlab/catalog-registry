"""Connect only to globally routable addresses, and to the very address that was checked.

Checking a name and then letting the HTTP client resolve it again leaves a gap: a hostile DNS server
answers a public address to the check and `127.0.0.1` to the connect (DNS rebinding). Here the
resolution, the check and the connect are one step, inside httpcore's network backend, so there is
no second lookup to race. TLS still verifies the certificate against the host name: httpcore passes
the request's host as `server_hostname`, whatever address the socket went to.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend

from registry.domain.fetching import FetchError
from registry.domain.url_policy import is_public_address

#: (host, port) → the addresses to try, in order.
Resolver = Callable[[str, int], Awaitable[list[str]]]


async def lookup_host_addresses(host: str, port: int) -> list[str]:
    """Every address *host* resolves to, by the system resolver, without blocking the loop."""
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise FetchError("host-unresolvable", "The host name could not be found.") from None
    return list(dict.fromkeys(str(answer[4][0]) for answer in answers))


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """An httpcore backend that refuses any host with a non-public address.

    All addresses must be public, not just the first: a name answering one public and one private
    address would otherwise reach the private one whenever the first fails.
    """

    def __init__(self, resolver: Resolver = lookup_host_addresses) -> None:
        self._resolve = resolver
        self._inner = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._resolve(host, port)
        if not addresses:
            raise FetchError("host-unresolvable", "The host name could not be found.")
        for address in addresses:
            if not is_public_address(ipaddress.ip_address(address.split("%", 1)[0])):
                raise FetchError(
                    "url-unsafe", "The host name points to a private or reserved network."
                )
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise FetchError("url-unsafe", "Unix sockets are never used for outbound requests.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PublicOnlyTransport(httpx.AsyncHTTPTransport):
    """httpx's transport, with its connection pool on `PublicOnlyBackend`.

    httpx does not expose the network backend, so the pool is rebuilt with the same TLS settings.
    `test_public_backend.py` fails if a future httpx changes that attribute.
    """

    def __init__(self, resolver: Resolver = lookup_host_addresses) -> None:
        super().__init__()
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            network_backend=PublicOnlyBackend(resolver),
        )
