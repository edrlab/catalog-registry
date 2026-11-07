"""Errors rendered as RFC 9457 problem documents, handler by handler."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from registry.api.exception_handlers import (
    build_problem_instance,
    handle_http_exception,
    handle_registry_error,
    handle_unexpected_error,
    handle_validation_error,
)

pytestmark = pytest.mark.unit

Handler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def build_request(path: str = "/search", state: dict[str, Any] | None = None) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
    }
    if state is not None:
        scope["state"] = state
    return Request(scope)


def test_a_request_without_an_id_names_no_occurrence() -> None:
    assert build_problem_instance(build_request(state={})) is None


async def test_an_unexpected_error_without_a_request_id_sends_no_empty_request_id_header() -> None:
    response = await handle_unexpected_error(build_request(state={}), RuntimeError("boom"))

    assert "x-request-id" not in response.headers


async def test_a_status_with_no_standard_phrase_is_titled_error() -> None:
    """RFC 9457 §4.2.1 takes the title from the status phrase; a status without one gets a
    generic title rather than an error inside the error handler."""
    response = await handle_http_exception(
        build_request(), HTTPException(status_code=499, detail="Client went away")
    )

    assert json.loads(bytes(response.body))["title"] == "Error"


@pytest.mark.parametrize(
    "handler", [handle_registry_error, handle_http_exception, handle_validation_error]
)
async def test_a_handler_given_an_error_it_does_not_render_lets_it_go_on(handler: Handler) -> None:
    """Each handler answers for one family of errors; anything else goes to the next handler."""
    with pytest.raises(LookupError):
        await handler(build_request(), LookupError("not mine"))
