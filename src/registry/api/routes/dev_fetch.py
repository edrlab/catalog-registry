"""Read one OPDS document from a library's own server, for the dev console (ADR-063).

This route is mounted only when `REGISTRY_ENVIRONMENT=local` (see `create_app`), so none of the
following applies in a deployed environment. Should it ever be mounted elsewhere, two limits keep it
from being an open relay on a public service:

* **Who:** outside `local` the caller must send the shared secret in `X-Dev-Token`
  (`REGISTRY_DEV_FETCH_TOKEN`). With none configured the route answers 404.
* **Where:** outside `local` only hosts that appear in a registered catalog's `catalog` link.

On top of both: public http(s) hosts only, every redirect hop checked, size and time capped.
`httpx` is a development dependency, so an image built without dev dependencies cannot serve it.
"""

import asyncio
import hmac
import ipaddress
import json
import socket
import time
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from registry.api.routes.dev import CLIENTS
from registry.core.config import Settings

router = APIRouter(prefix="/dev", tags=["dev"], include_in_schema=False)

#: What `/dev/fetch` will read from a library's own server: nothing larger, nothing slower, and no
#: more redirects than a feed normally needs.
FETCH_MAX_BYTES: Final = 2_000_000
FETCH_TIMEOUT_SECONDS: Final = 10.0
FETCH_MAX_REDIRECTS: Final = 3


def authorize_fetch(settings: Settings, supplied_token: str | None) -> bool:
    """Who may call. Returns True when the caller must also stay inside the registered hosts.

    Local: anyone on the machine, any public host. Anywhere else the route needs a shared secret
    (`REGISTRY_DEV_FETCH_TOKEN`). With none configured the route does not exist (404); with a wrong
    or missing one it answers 401. The comparison is constant-time (OWASP: timing-safe secret
    checks).
    """
    if settings.environment == "local":
        return False
    expected = settings.dev_fetch_token
    if expected is None:
        raise HTTPException(status_code=404)
    given = (supplied_token or "").encode()
    if not hmac.compare_digest(given, expected.get_secret_value().encode()):
        raise HTTPException(status_code=401, detail="a valid X-Dev-Token header is required")
    return True


async def read_registered_hosts(request: Request) -> frozenset[str]:
    """Where a deployed `/dev/fetch` may go: the hosts of the feeds the registry lists."""
    async with request.app.state.open_catalog_reader() as reader:
        hosts: frozenset[str] = await reader.fetch_registered_hosts()
    return hosts


def require_registered_host(url: str, allowed: frozenset[str] | None) -> None:
    """Outside local, only a host that appears in a registered catalog link may be fetched. Checked
    on every redirect hop too, so a registered library cannot bounce the request elsewhere."""
    if allowed is not None and (urlsplit(url).hostname or "") not in allowed:
        raise HTTPException(status_code=403, detail="that host is not a registered library")


async def require_public_host(url: str) -> None:
    """Refuse anything but a public http(s) host: allow-list the scheme, resolve the name, reject
    every non-global address (OWASP SSRF prevention).

    `/dev/fetch` runs on a developer's machine, where `localhost`, the LAN and the cloud metadata
    address are all reachable. A feed URL comes from data this tool does not control, so it must not
    be able to point the request at them. Residual risk: the name is resolved again when the
    connection is made (DNS rebinding); acceptable for a local-only tool, not for anything deployed.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise HTTPException(status_code=422, detail="only http and https URLs can be fetched")
    try:
        found = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname,
            parts.port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise HTTPException(status_code=502, detail=f"cannot resolve {parts.hostname}") from error
    if not all(ipaddress.ip_address(item[4][0]).is_global for item in found):
        raise HTTPException(status_code=422, detail="that host is not a public address")


async def fetch_public(
    url: str,
    headers: Mapping[str, str],
    *,
    allowed_hosts: frozenset[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[int, dict[str, str], bytes, int]:
    """GET *url*, checking every hop. Returns status, headers, decoded body, bytes on the wire.

    The 10 seconds are a budget for the whole fetch, redirects included. httpx's own timeout limits
    each read, so a server that trickles one byte at a time would never trip it.
    """
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            return await _fetch_hops(url, headers, allowed_hosts, transport)
    except TimeoutError as error:
        raise HTTPException(
            status_code=502, detail=f"the feed took longer than {FETCH_TIMEOUT_SECONDS:g} s"
        ) from error


async def _fetch_hops(
    url: str,
    headers: Mapping[str, str],
    allowed_hosts: frozenset[str] | None,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[int, dict[str, str], bytes, int]:
    sends_encoding = any(name.lower() == "accept-encoding" for name in headers)
    async with httpx.AsyncClient(
        transport=transport, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False
    ) as http:
        for _ in range(FETCH_MAX_REDIRECTS + 1):
            require_registered_host(url, allowed_hosts)
            await require_public_host(url)
            request = http.build_request("GET", url, headers=dict(headers))
            if not sends_encoding:
                # httpx adds its own Accept-Encoding. A client profile that sends none (curl, a
                # bare .NET client) must go out without one, or the simulation is httpx's.
                request.headers.pop("accept-encoding", None)
            try:
                response = await http.send(request, stream=True)
                try:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > FETCH_MAX_BYTES:
                            raise HTTPException(status_code=502, detail="the feed is over 2 MB")
                    wire = response.num_bytes_downloaded
                    status = response.status_code
                    response_headers = dict(response.headers)
                finally:
                    await response.aclose()
            except httpx.HTTPError as error:
                raise HTTPException(status_code=502, detail=f"fetch failed: {error!r}") from error
            location = response_headers.get("location")
            if status in {301, 302, 303, 307, 308} and location:
                url = urljoin(url, location)
                continue
            return status, response_headers, bytes(body), wire
    raise HTTPException(status_code=502, detail="too many redirects")


def build_fetch_headers(profile: Mapping[str, Any], language: str) -> dict[str, str]:
    """A client's own headers, exactly, plus the Accept a feed reader sends."""
    headers = {"Accept": "application/opds+json, application/json;q=0.9, */*;q=0.1"}
    headers.update(profile["headers"])
    if language:
        headers["Accept-Language"] = language
    return headers


@router.get("/fetch")
async def fetch_feed(
    request: Request,
    url: str = Query(max_length=2000),
    client: str = Query(default="thorium"),
    language: str = Query(default="", max_length=120),
) -> JSONResponse:
    """Read one OPDS document from a library's own server, as *client* would ask for it."""
    restricted = authorize_fetch(request.app.state.settings, request.headers.get("x-dev-token"))
    profile = CLIENTS.get(client)
    if profile is None:
        raise HTTPException(status_code=422, detail=f"unknown client {client!r}")
    headers = build_fetch_headers(profile, language)

    started = time.perf_counter()
    allowed = await read_registered_hosts(request) if restricted else None
    status, response_headers, body, wire = await fetch_public(url, headers, allowed_hosts=allowed)
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        parsed: Any = json.loads(body)
        is_json = True
    except ValueError:
        parsed, is_json = body.decode("utf-8", "replace")[:20_000], False
    return JSONResponse(
        {
            "client": client,
            "request": {"method": "GET", "path": url, "headers": headers},
            "status": status,
            "headers": {k.lower(): v for k, v in response_headers.items()},
            "encoding": response_headers.get("content-encoding"),
            "wire_bytes": wire,
            "decoded_bytes": len(body),
            "ms": round(elapsed_ms, 1),
            "is_json": is_json,
            "body": parsed,
        },
        headers={"Cache-Control": "no-store"},
    )
