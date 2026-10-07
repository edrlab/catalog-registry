"""Response compression by negotiation (ADR-061).

The client says what it can decode in `Accept-Encoding` (RFC 9110 §12.5.3); this picks from what
the server offers. Measured on real clients (docs/search.md): gzip is the only coding every client
sends by default; Thorium Reader and browsers also offer `br`; recent browsers add `zstd`; KOReader
sends `identity` and must get the body as it is. So: `zstd` if the client names it, else `br`, else
`gzip`, nothing when it asks for nothing.

Levels are the measured sweet spot for a response made while the client waits: zstd 6, brotli 4 and
gzip 6. Never brotli 11 (25 ms for a 27 KB page) or gzip 9. zstd 6 compresses in about 0.02 ms
where brotli 4 takes 0.07, for 2% more bytes on the real feed, and only a client that names it
ever gets it (RFC 8878).
"""

import gzip
import json
import logging
import sys
from typing import Final

import brotli
import zstandard
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from registry.api.exception_handlers import build_problem_response

#: In the server's order of preference, used to break a tie between equal q-values.
OFFERED: Final = ("zstd", "br", "gzip")

#: Below this the header costs more than the compression saves.
MINIMUM_SIZE: Final = 1024

BROTLI_QUALITY: Final = 4
ZSTD_LEVEL: Final = 6
GZIP_LEVEL: Final = 6

_USER_AGENT_LIMIT: Final = 200

#: One compressor for every request: compression runs synchronously on the event loop, so two
#: requests never use it at once.
_ZSTD: Final = zstandard.ZstdCompressor(level=ZSTD_LEVEL)


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
    `*` stands for every coding not named, except `zstd`: a client has to name it, because a
    wildcard is no proof that it can decode a coding this new. A coding with q=0 is refused.
    """
    wanted = parse_accept_encoding(header)
    best: str | None = None
    best_quality = 0.0
    for coding in OFFERED:
        quality = wanted.get(coding, 0.0 if coding == "zstd" else wanted.get("*", 0.0))
        if quality > best_quality:
            best, best_quality = coding, quality
    return best


def identity_acceptable(header: str | None) -> bool:
    """Whether the client accepts an uncoded body (RFC 9110 §12.5.3).

    Yes when there is no header, or an empty one. No when `identity` is refused with q=0, or when
    `*` is refused with q=0 and `identity` is not named. Anything else keeps identity acceptable,
    even a list that names only codings we do not offer.
    """
    if header is None:
        return True
    wanted = parse_accept_encoding(header)
    if "identity" in wanted:
        return wanted["identity"] > 0
    return wanted.get("*", 1.0) > 0


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


def log_client_request(
    scope: Scope, request_headers: Headers, *, status: int, encoding: str | None
) -> None:
    """The one line per request, whichever path the response took (ADR-061)."""
    _client_log.info(
        json.dumps(
            {
                "severity": "INFO",
                "message": "client",
                "path": scope["path"],
                "status": status,
                "accept_encoding": request_headers.get("accept-encoding"),
                "encoding": encoding,
                "user_agent": (request_headers.get("user-agent") or "")[:_USER_AGENT_LIMIT],
            },
            ensure_ascii=False,
        )
    )


def _add_vary(headers: MutableHeaders) -> None:
    """`Vary: Accept-Encoding`, kept next to whatever the inner app already varies on."""
    headers.setdefault("vary", "Accept-Encoding")
    if "accept-encoding" not in headers["vary"].lower():
        headers["vary"] = f"{headers['vary']}, Accept-Encoding"


def _compress(body: bytes, encoding: str) -> bytes:
    compressed: bytes
    if encoding == "zstd":
        compressed = _ZSTD.compress(body)
    elif encoding == "br":
        compressed = brotli.compress(body, quality=BROTLI_QUALITY)
    else:
        compressed = gzip.compress(body, compresslevel=GZIP_LEVEL, mtime=0)
    return compressed


class CompressionMiddleware:
    """Compress a finished response when the client can take it, and log who asked.

    The whole body is buffered: every response here is a JSON document, a few hundred KB at most.

    A HEAD response carries the headers of the GET it stands for (its `Content-Length` is the size
    of the body it does not send), so it is passed through untouched except for `Vary`.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_headers = Headers(scope=scope)
        offered = request_headers.get("accept-encoding")
        encoding = choose_encoding(offered)
        if encoding is None and not identity_acceptable(offered):
            # The client refused every coding we offer and refused an uncoded body too (RFC 9110
            # §12.5.3). Nothing acceptable exists, so say so instead of sending what it refused.
            refusal = build_problem_response(
                status=406,
                title="Not Acceptable",
                detail="Accept-Encoding refuses identity and every coding offered: zstd, br, gzip.",
            )
            _add_vary(refusal.headers)
            log_client_request(scope, request_headers, status=406, encoding=None)
            await refusal(scope, receive, send)
            return

        start: Message | None = None
        chunks: list[bytes] = []
        logged = False
        is_head = scope.get("method") == "HEAD"

        async def respond(message: Message) -> None:
            nonlocal start, logged
            if is_head:
                if message["type"] == "http.response.start":
                    _add_vary(MutableHeaders(scope=message))
                    log_client_request(
                        scope, request_headers, status=message["status"], encoding=None
                    )
                    logged = True
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
                and len(body) >= MINIMUM_SIZE
                and "content-encoding" not in headers
            ):
                # gzip with mtime=0: the same body always gives the same bytes, so a cache can
                # hold one copy.
                body = _compress(body, encoding)
                headers["content-encoding"] = encoding
                applied = encoding
            headers["content-length"] = str(len(body))
            log_client_request(scope, request_headers, status=start["status"], encoding=applied)
            logged = True
            await send(start)
            await send({"type": "http.response.body", "body": body})

        try:
            await self.app(scope, receive, respond)
        except Exception:
            # Starlette sends an unexpected error's 500 from outside every middleware, so this is
            # the last place that still knows the request. Log it, then let the error go on.
            if not logged:
                log_client_request(scope, request_headers, status=500, encoding=None)
            raise
