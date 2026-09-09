"""Request-id middleware. Echoes a caller-supplied id, or mints one."""

import uuid

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from registry.core.constants import HEADER_REQUEST_ID


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get(HEADER_REQUEST_ID) or str(uuid.uuid4())

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"].append(
                    (HEADER_REQUEST_ID.encode("latin-1"), request_id.encode("latin-1"))
                )
            await send(message)

        await self.app(scope, receive, send_with_request_id)
