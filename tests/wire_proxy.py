"""A TCP proxy on loopback that counts the round trips a client makes to PostgreSQL.

What costs time when the database is a region away is the number of times the app sends something
and then waits for the answer. A statement counter cannot see the ones the driver sends itself
(pre-ping, transaction control, type look-ups), so the tests put this between the engine and the
server and watch the wire.

TCP is a byte stream: one `read` can return half a protocol message or several writes joined, so
reads are not messages and are not counted. A **round trip** is a run of client bytes followed by
the server's reply. Client data that arrives before the server has answered belongs to the same
round trip, however the network happened to cut it. This is also what a pipelined driver costs in
latency: one wait, however many messages it sent in a row.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from sqlalchemy.engine import make_url

from registry.core.config import Settings


@dataclass
class WireLog:
    """The client's round trips, in order. Each entry is every byte the client sent in that one."""

    flights: list[bytes] = field(default_factory=list)
    _server_has_answered: bool = True  # so the first client bytes start a round trip

    def client_sent(self, data: bytes) -> None:
        if self._server_has_answered:
            self.flights.append(data)
            self._server_has_answered = False
        else:
            self.flights[-1] += data

    def server_sent(self) -> None:
        self._server_has_answered = True

    @property
    def round_trips(self) -> int:
        return len(self.flights)

    def mark(self) -> int:
        return len(self.flights)

    def since(self, mark: int) -> list[bytes]:
        return self.flights[mark:]

    def contains(self, *needles: bytes, since: int = 0) -> bool:
        return any(needle in flight for flight in self.flights[since:] for needle in needles)


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    note: Callable[[bytes], None],
) -> None:
    try:
        while data := await reader.read(65536):
            note(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


@contextlib.asynccontextmanager
async def counting_proxy(settings: Settings) -> AsyncIterator[tuple[Settings, WireLog]]:
    """Settings that reach the same database through the proxy, and the log of what they sent."""
    target = make_url(str(settings.database_url))
    log = WireLog()
    tasks: set[asyncio.Task[None]] = set()

    async def handle(
        client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        server_reader, server_writer = await asyncio.open_connection(target.host, target.port)
        upstream = asyncio.create_task(_pipe(client_reader, server_writer, log.client_sent))
        downstream = asyncio.create_task(
            _pipe(server_reader, client_writer, lambda _data: log.server_sent())
        )
        tasks.update((upstream, downstream))
        await asyncio.wait((upstream, downstream), return_when=asyncio.FIRST_COMPLETED)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    through = target.set(host="127.0.0.1", port=port).render_as_string(hide_password=False)
    try:
        yield settings.model_copy(update={"database_url": through}), log
    finally:
        server.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()
