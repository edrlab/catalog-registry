"""Response compression by negotiation (ADR-061).

The client says what it can decode in `Accept-Encoding` (RFC 9110 §12.5.3); this picks from what
the server offers. Measured on real clients (docs/search.md): gzip is the only coding every client
sends by default; Thorium Reader and browsers also offer `br`; KOReader sends `identity` and must
get the body as it is. So: `br` when the client prefers it, `gzip` otherwise, nothing when it asks
for nothing.

Levels are the measured sweet spot for a response made while the client waits: brotli 4 and gzip 6.
Never brotli 11 (25 ms for a 27 KB page) or gzip 9. `zstd` is not offered: only recent browsers
send it.
"""

import gzip
import json
import logging
import sys
from typing import Final

import brotli
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: In the server's order of preference, used to break a tie between equal q-values.
OFFERED: Final = ("br", "gzip")

#: Below this the header costs more than the compression saves.
MINIMUM_SIZE: Final = 1024

BROTLI_QUALITY: Final = 4
GZIP_LEVEL: Final = 6

_NO_BODY_STATUSES: Final = frozenset({204, 304})
_USER_AGENT_LIMIT: Final = 200


def parse_accept_encoding(header: str | None) -> dict[str, float]:
    """`gzip;q=0.5, br` becomes `{"gzip": 0.5, "br": 1.0}`. Names are lowercased.

    A token with an unreadable q-value is dropped rather than guessed at, and q is clamped to 0..1.
    """
    codings: dict[str, float] = {}
    for part in (header or "").split(","):
        name, *params = (piece.strip() for piece in part.split(";"))
        name = name.lower()
        if not name:
            continue
        quality, readable = 1.0, True
        for param in params:
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = min(1.0, max(0.0, float(value)))
                except ValueError:
                    readable = False
        if readable:
            codings[name] = quality
    return codings


def choose_encoding(header: str | None) -> str | None:
    """The coding to answer with, or `None` to send the body as it is.

    No header, `identity`, or only codings we do not offer all mean `None`: an absent header is read
    as identity (the safest reading; RFC 9110 allows more), and `identity` is what KOReader sends.
    `*` stands for every coding not named. A coding with q=0 is refused.
    """
    wanted = parse_accept_encoding(header)
    best: str | None = None
    best_quality = 0.0
    for coding in OFFERED:
        quality = wanted.get(coding, wanted.get("*", 0.0))
        if quality > best_quality:
            best, best_quality = coding, quality
    return best


def compress(body: bytes, encoding: str) -> bytes:
    if encoding == "br":
        compressed: bytes = brotli.compress(body, quality=BROTLI_QUALITY)
        return compressed
    # mtime=0: the same body always gives the same bytes, so a cache can hold one copy.
    return gzip.compress(body, compresslevel=GZIP_LEVEL, mtime=0)


_client_log = logging.getLogger("registry.clients")


def configure_client_log() -> None:
    """One JSON line per request on stdout, which Cloud Run turns into a log entry.

    Cloud Logging reads `severity` and `message` from a JSON line and keeps the rest as fields, so
    clients can be counted by encoding and user agent after rollout (docs/search.md). No address,
    no query string, no header but these two.
    """
    if _client_log.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _client_log.addHandler(handler)
    _client_log.setLevel(logging.INFO)
    _client_log.propagate = False


def _add_vary(headers: MutableHeaders) -> None:
    """`Vary: Accept-Encoding`, kept next to whatever the inner app already varies on."""
    headers.setdefault("vary", "Accept-Encoding")
    if "accept-encoding" not in headers["vary"].lower():
        headers["vary"] = f"{headers['vary']}, Accept-Encoding"


class CompressionMiddleware:
    """Compress a finished response when the client can take it, and log who asked.

    The whole body is buffered: every response here is a JSON document, a few hundred KB at most.

    A HEAD response carries the headers of the GET it stands for (its `Content-Length` is the size
    of the body it does not send), so it is passed through untouched except for `Vary`.
    """

    def __init__(self, app: ASGIApp, *, minimum_size: int = MINIMUM_SIZE) -> None:
        self.app = app
        self.minimum_size = minimum_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_headers = Headers(scope=scope)
        offered = request_headers.get("accept-encoding")
        encoding = choose_encoding(offered)
        start: Message | None = None
        chunks: list[bytes] = []

        is_head = scope.get("method") == "HEAD"

        async def respond(message: Message) -> None:
            nonlocal start
            if is_head:
                if message["type"] == "http.response.start":
                    _add_vary(MutableHeaders(scope=message))
                await send(message)
                return
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return

            body = b"".join(chunks)
            headers = MutableHeaders(scope=start)
            _add_vary(headers)
            applied = None
            if (
                encoding is not None
                and start["status"] not in _NO_BODY_STATUSES
                and len(body) >= self.minimum_size
                and "content-encoding" not in headers
                and "no-transform" not in headers.get("cache-control", "").lower()
            ):
                body = compress(body, encoding)
                headers["content-encoding"] = encoding
                applied = encoding
            headers["content-length"] = str(len(body))
            _client_log.info(
                json.dumps(
                    {
                        "severity": "INFO",
                        "message": "client",
                        "path": scope["path"],
                        "status": start["status"],
                        "accept_encoding": offered,
                        "encoding": applied,
                        "user_agent": (request_headers.get("user-agent") or "")[:_USER_AGENT_LIMIT],
                    },
                    ensure_ascii=False,
                )
            )
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, respond)
