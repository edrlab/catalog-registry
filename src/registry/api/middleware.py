"""Response headers: the request id, and the static security set.

One middleware rather than two. Both wrap `send` to add headers to the response, and a second
ASGI wrapper per request buys nothing but another frame.
"""

import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from registry.core.constants import HEADER_REQUEST_ID

#: `nosniff` because JSON must never be sniffed into something
#: executable; `Referrer-Policy` because a catalog URL names a library a reader borrows from,
#: and that path should not travel in a `Referer`; `X-Frame-Options` because nothing here is
#: meant to be framed and the back office (v1.0) must not be.
#:
#: `Strict-Transport-Security` is absent deliberately, it belongs at the ingress, and sending
#: it over plain HTTP in development teaches a browser to refuse `http://localhost`.
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
}


ADMIN_PATH_PREFIX = "/admin"


def is_admin_path(path: str) -> bool:
    return path == ADMIN_PATH_PREFIX or path.startswith(f"{ADMIN_PATH_PREFIX}/")


class PublicCorsMiddleware(CORSMiddleware):
    """CORS for the public reads only. `/admin` is called server to server, never by a browser, so
    it gets no `Access-Control-*` headers at all, whatever `Origin` a request carries."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_admin_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


class ResponseHeadersMiddleware:
    """Echoes a caller-supplied request id or mints one, and sets the security headers. Admin
    responses are also `Cache-Control: no-store`: they are per person and never shareable."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get(HEADER_REQUEST_ID) or str(uuid.uuid4())
        no_store = is_admin_path(scope["path"])
        # For the problem document's `instance`, which is built where the response is.
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                headers[HEADER_REQUEST_ID] = request_id
                if no_store:
                    headers["cache-control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)
