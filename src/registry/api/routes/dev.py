"""The dev console: a page to try search and the feed as the real reader apps would (ADR-063).

**Local only.** `create_app` mounts this router only when `REGISTRY_ENVIRONMENT=local`, and imports
the module only then; the handlers check again, so a mistake in one place does not expose it. It is
not in any deployed environment: production and staging answer 404 for `/dev`.

A browser cannot set `Accept-Encoding` or `User-Agent`, so a plain page cannot be KOReader. This
asks the server to call the app's own ASGI stack, in-process, with a client's exact headers.
There is no network and no URL to follow (no SSRF): the path is checked against a short
allow-list, and the call goes through the real middleware, so compression and the client log
behave as they would.
"""

import gzip
import json
import time
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import brotli
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Message

router = APIRouter(prefix="/dev", tags=["dev"], include_in_schema=False)

#: What each reader sends. `source` is `measured` where the headers were read from the client's own
#: source or captured from a real run (docs/performance.md), `typical` where they are the common
#: defaults and not verified. The User-Agent strings are examples. The browser row adds `zstd`,
#: which the server does not offer, so it must be ignored.
CLIENTS: Final[dict[str, dict[str, Any]]] = {
    "thorium": {
        "label": "Thorium Reader (Electron, node-fetch)",
        "source": "measured",
        "headers": {"Accept-Encoding": "gzip, deflate, br", "User-Agent": "Thorium Reader/3.5"},
    },
    "koreader": {
        "label": "KOReader (Kobo, Kindle, PocketBook, Android)",
        "source": "measured",
        "headers": {"Accept-Encoding": "identity", "User-Agent": "KOReader/2025.10"},
    },
    "urlsession": {
        "label": "iOS / macOS app (URLSession, Readium Swift)",
        "source": "measured",
        "headers": {"Accept-Encoding": "gzip, deflate", "User-Agent": "Reader/1.0 CFNetwork"},
    },
    "android": {
        "label": "Android app (HttpURLConnection, Readium Kotlin)",
        "source": "typical",
        "headers": {"Accept-Encoding": "gzip", "User-Agent": "Dalvik/2.1.0 (Linux; Android 14)"},
    },
    "windows": {
        "label": "Windows app (.NET HttpClient, no decompression set)",
        "source": "typical",
        "headers": {"User-Agent": "ReaderApp/1.0 .NET"},
    },
    "browser": {
        "label": "Browser (Chrome, Firefox, Safari)",
        "source": "typical",
        "headers": {
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/130 Safari/537.36",
        },
    },
    "requests": {
        "label": "Python requests / httpx",
        "source": "typical",
        "headers": {"Accept-Encoding": "gzip, deflate", "User-Agent": "python-requests/2.32"},
    },
    "curl": {
        "label": "curl (no flags)",
        "source": "typical",
        "headers": {"User-Agent": "curl/8.7"},
    },
}

#: Only the public reads. No `/dev` (no recursion), no health, no docs.
_PATH: Final = r"^/(search(\?[^#\s]*)?|catalogs/[0-9a-fA-F-]{36})?$"

_HTML: Final = Path(__file__).resolve().parents[1] / "dev_console.html"

#: The page loads nothing from outside and talks only to this server.
_CSP: Final = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'"
)


def _require_local(request: Request) -> None:
    if request.app.state.settings.environment != "local":
        raise HTTPException(status_code=404)


async def call_app(
    app: ASGIApp, path: str, headers: Mapping[str, str]
) -> tuple[int, dict[str, str], bytes]:
    """GET *path* through *app*'s whole ASGI stack. Returns status, headers, the raw body bytes."""
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 0),
        "server": ("localhost", 8000),
        "app": app,
    }
    status = 500
    response_headers: dict[str, str] = {}
    chunks: list[bytes] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            response_headers.update(
                (k.decode().lower(), v.decode()) for k, v in message.get("headers", [])
            )
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status, response_headers, b"".join(chunks)


def decode(body: bytes, encoding: str | None) -> bytes:
    if encoding == "br":
        decoded: bytes = brotli.decompress(body)
        return decoded
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    return body


@router.get("", response_class=HTMLResponse)
async def read_console(request: Request) -> HTMLResponse:
    _require_local(request)
    return HTMLResponse(
        _HTML.read_text(encoding="utf-8"),
        headers={"Content-Security-Policy": _CSP, "Cache-Control": "no-store"},
    )


@router.get("/clients")
async def read_clients(request: Request) -> JSONResponse:
    _require_local(request)
    return JSONResponse(
        {
            key: {"label": c["label"], "source": c["source"], "headers": c["headers"]}
            for key, c in CLIENTS.items()
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/simulate")
async def simulate(
    request: Request,
    path: str = Query(pattern=_PATH, max_length=600),
    client: str = Query(default="thorium"),
    language: str = Query(default="", max_length=120),
) -> JSONResponse:
    """Make one request as *client* would, and report what came back and what it cost."""
    _require_local(request)
    profile = CLIENTS.get(client)
    if profile is None:
        raise HTTPException(status_code=422, detail=f"unknown client {client!r}")
    headers = {"Accept": "application/opds+json, application/json", **profile["headers"]}
    if language:
        headers["Accept-Language"] = language

    started = time.perf_counter()
    status, response_headers, wire = await call_app(request.app, path, headers)
    elapsed_ms = (time.perf_counter() - started) * 1000

    encoding = response_headers.get("content-encoding")
    decoded = decode(wire, encoding)
    try:
        body: Any = json.loads(decoded)
        is_json = True
    except ValueError:
        body, is_json = decoded.decode("utf-8", "replace"), False
    return JSONResponse(
        {
            "client": client,
            "request": {"method": "GET", "path": path, "headers": headers},
            "status": status,
            "headers": response_headers,
            "encoding": encoding,
            "wire_bytes": len(wire),
            "decoded_bytes": len(decoded),
            "ms": round(elapsed_ms, 1),
            "is_json": is_json,
            "body": body,
        },
        headers={"Cache-Control": "no-store"},
    )
