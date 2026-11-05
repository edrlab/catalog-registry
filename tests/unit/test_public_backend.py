"""`PublicOnlyBackend`: resolve, check and connect in one step, so DNS rebinding cannot race."""

from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
import pytest

from registry.domain.fetching import FetchError
from registry.network.public_backend import PublicOnlyBackend, PublicOnlyTransport


class RecordingBackend:
    """Stands in for the real socket layer: records where a connection would go."""

    def __init__(self, failing: Iterable[str] = ()) -> None:
        self.connected: list[tuple[str, int]] = []
        self.failing = set(failing)

    async def connect_tcp(self, host: str, port: int, *_: Any, **__: Any) -> Any:
        self.connected.append((host, port))
        if host in self.failing:
            raise httpcore.ConnectError("refused")
        return object()


def backend_with(answers: list[list[str]], inner: RecordingBackend) -> PublicOnlyBackend:
    """A backend whose resolver gives each answer in turn: a rebinding DNS server."""
    calls = iter(answers)

    async def resolve(_host: str, _port: int) -> list[str]:
        return next(calls)

    backend = PublicOnlyBackend(resolve)
    backend._inner = inner  # type: ignore[assignment]  # the socket layer is what is under test
    return backend


async def test_it_connects_to_the_address_it_checked() -> None:
    inner = RecordingBackend()
    backend = backend_with([["93.184.215.14"], ["127.0.0.1"]], inner)

    await backend.connect_tcp("feeds.example.org", 443)

    assert inner.connected == [("93.184.215.14", 443)]


async def test_a_rebinding_answer_on_the_next_connection_is_refused() -> None:
    inner = RecordingBackend()
    backend = backend_with([["93.184.215.14"], ["127.0.0.1"]], inner)
    await backend.connect_tcp("feeds.example.org", 443)

    with pytest.raises(FetchError) as refused:
        await backend.connect_tcp("feeds.example.org", 443)

    assert refused.value.code == "url-unsafe"
    assert inner.connected == [("93.184.215.14", 443)]


@pytest.mark.parametrize(
    "answer",
    [
        ["93.184.215.14", "10.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["fe80::1%eth0"],
    ],
)
async def test_any_private_address_in_the_answer_refuses_the_host(answer: list[str]) -> None:
    inner = RecordingBackend()
    backend = backend_with([answer], inner)

    with pytest.raises(FetchError) as refused:
        await backend.connect_tcp("feeds.example.org", 443)

    assert refused.value.code == "url-unsafe"
    assert inner.connected == []


async def test_an_empty_answer_is_unresolvable() -> None:
    backend = backend_with([[]], RecordingBackend())

    with pytest.raises(FetchError) as refused:
        await backend.connect_tcp("nowhere.example", 443)

    assert refused.value.code == "host-unresolvable"


async def test_the_next_public_address_is_tried_when_one_refuses() -> None:
    inner = RecordingBackend(failing=["93.184.215.14"])
    backend = backend_with([["93.184.215.14", "93.184.215.15"]], inner)

    await backend.connect_tcp("feeds.example.org", 443)

    assert inner.connected == [("93.184.215.14", 443), ("93.184.215.15", 443)]


async def test_unix_sockets_are_never_used() -> None:
    with pytest.raises(FetchError):
        await PublicOnlyBackend().connect_unix_socket("/var/run/docker.sock")


async def test_the_transport_really_uses_the_public_only_backend() -> None:
    """httpx does not expose the backend; this fails if a new httpx renames the pool attribute."""
    transport = PublicOnlyTransport()

    assert isinstance(transport._pool._network_backend, PublicOnlyBackend)
    await transport.aclose()


async def test_a_real_request_to_loopback_is_refused_before_connecting() -> None:
    async with httpx.AsyncClient(transport=PublicOnlyTransport()) as client:
        with pytest.raises(FetchError) as refused:
            await client.get("http://localhost:1/")

    assert refused.value.code == "url-unsafe"
