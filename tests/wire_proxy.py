"""A TCP proxy on loopback that counts what a client sends to PostgreSQL.

The number of messages a search sends is the number of times the app waits for the database, which
is what costs time when the database is a region away. A statement counter cannot see the ones
the driver sends itself (pre-ping, transaction control, type look-ups), so the tests put this
between the engine and the server and count chunks on the wire. A chunk is what one `read` on the
client's socket returned: asyncpg writes each protocol message group in one `write`, and on
loopback those arrive whole.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from sqlalchemy.engine import make_url

from registry.core.config import Settings


@dataclass
class WireLog:
    """Client-to-server chunks, in order."""

    chunks: list[bytes] = field(default_factory=list)

    @property
    def to_server(self) -> int:
        return len(self.chunks)

    def mark(self) -> int:
        return len(self.chunks)

    def since(self, mark: int) -> list[bytes]:
        return self.chunks[mark:]

    def contains(self, *needles: bytes, since: int = 0) -> bool:
        return any(needle in chunk for chunk in self.chunks[since:] for needle in needles)


async def _pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, log: WireLog | None
) -> None:
    try:
        while data := await reader.read(65536):
            if log is not None:
                log.chunks.append(data)
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
        upstream = asyncio.create_task(_pipe(client_reader, server_writer, log))
        downstream = asyncio.create_task(_pipe(server_reader, client_writer, None))
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
