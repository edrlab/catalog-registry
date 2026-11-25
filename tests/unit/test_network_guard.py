"""The suite cannot reach the internet, the live registry or a hosted database.

`refuse_remote_network` (conftest) runs in every test process. These prove it is installed and
refuses before any packet leaves: a name lookup, a connect to an address literal, and the async
path the application's clients (asyncpg, httpx) take.
"""

import asyncio
import socket

import pytest

pytestmark = pytest.mark.unit

#: pytest loads conftest under its own module name, so the class is matched by its message.
REFUSED = "a test tried to"


@pytest.mark.parametrize("host", ["registry.thoriumreader.com", "aws.connect.psdb.cloud"])
def test_a_remote_name_is_not_even_looked_up(host: str) -> None:
    with pytest.raises(RuntimeError, match=REFUSED):
        socket.getaddrinfo(host, 443)


def test_a_connect_to_a_remote_address_is_refused() -> None:
    # 192.0.2.0/24 is TEST-NET-1 (RFC 5737): never routed, so nothing could leave even unguarded.
    with socket.socket() as sock, pytest.raises(RuntimeError, match=REFUSED):
        sock.connect(("192.0.2.1", 443))


async def test_the_event_loop_path_is_refused_too() -> None:
    with pytest.raises(RuntimeError, match=REFUSED):
        await asyncio.open_connection("192.0.2.1", 443)


def test_localhost_still_resolves() -> None:
    assert socket.getaddrinfo("localhost", 5432)
