"""Domain errors and framework errors, rendered as RFC 9457 problem documents."""

import uuid
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from registry.api.admin_log import configure_admin_log, log_admin_refusal
from registry.api.middleware import SECURITY_HEADERS, is_admin_path
from registry.core.constants import PROBLEM_JSON_MEDIA_TYPE
from registry.core.errors import (
    AdminUnavailableError,
    AuthenticationError,
    PermissionDeniedError,
    RegistryError,
)


def build_problem_response(
    *,
    status: int,
    title: str,
    detail: str,
    problem_type: str = "about:blank",
    errors: list[dict[str, str]] | None = None,
    instance: str | None = None,
) -> JSONResponse:
    """RFC 9457 §3 problem details.

    The security headers are set here as well as in the middleware, and that is not
    redundant. Starlette always installs `ServerErrorMiddleware` outermost, so a 500 is sent
    past every middleware the application adds. Without this, the one response most likely to
    be inspected by hand is the one response with no `nosniff` on it.
    """
    content: dict[str, Any] = {
        "type": problem_type,
        "title": title,
        "status": status,
        "detail": detail,
    }
    if instance:
        content["instance"] = instance
    if errors:
        content["errors"] = errors
    return JSONResponse(
        status_code=status,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        content=content,
        headers=dict(SECURITY_HEADERS),
    )


def build_problem_instance(request: Request) -> str | None:
    """RFC 9457 §3.1.5: a URI naming this occurrence, so a body pasted into a ticket can be traced.

    The request id is the caller's `X-Request-Id` or one the middleware minted. It is a URN so it
    is a valid URI whatever the caller sent: a UUID keeps its natural form, anything else is
    percent-encoded.
    """
    request_id = request.scope.get("state", {}).get("request_id")
    if not request_id:
        return None
    try:
        return f"urn:uuid:{uuid.UUID(request_id)}"
    except ValueError:
        return f"urn:request-id:{quote(request_id, safe='')}"


def _without_caching(request: Request, response: JSONResponse) -> JSONResponse:
    """An admin error is as private as an admin success. The middleware cannot add this to a 500,
    which Starlette sends past it, so the handlers set it themselves."""
    if is_admin_path(request.url.path):
        response.headers["cache-control"] = "no-store"
    return response


async def handle_registry_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RegistryError):
        raise exc
    if is_admin_path(_request.url.path) and isinstance(
        exc, AuthenticationError | PermissionDeniedError | AdminUnavailableError
    ):
        log_admin_refusal(_request, status=exc.status_code, reason=str(exc))
    response = build_problem_response(
        status=exc.status_code,
        title=exc.title,
        detail=str(exc),
        problem_type=exc.problem_type,
        instance=build_problem_instance(_request),
    )
    if isinstance(exc, AuthenticationError):
        # RFC 9110 §15.5.2: a 401 must say how to authenticate.
        response.headers["www-authenticate"] = 'Registry-Service-Key realm="registry-admin"'
    return _without_caching(_request, response)


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        raise exc
    # RFC 9457 §4.2.1: with `about:blank` the title is the status phrase.
    try:
        title = HTTPStatus(exc.status_code).phrase
    except ValueError:
        title = "Error"
    detail = str(exc.detail)
    if detail == title:
        # Starlette's default repeats the title, which says nothing about this occurrence.
        detail = {
            404: f"No resource at {request.url.path}.",
            405: f"{request.method} is not allowed on {request.url.path}.",
        }.get(exc.status_code, detail)
    response = build_problem_response(
        status=exc.status_code,
        title=title,
        detail=detail,
        instance=build_problem_instance(request),
    )
    # A 405 must carry `Allow` (RFC 9110 §15.5.6); Starlette puts it on the exception.
    response.headers.update(exc.headers or {})
    return _without_caching(request, response)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """`/admin` answers RFC 9457 with the offending fields; the public API keeps FastAPI's 422."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    if not is_admin_path(request.url.path):
        return await request_validation_exception_handler(request, exc)
    errors = [
        {"field": ".".join(str(part) for part in error["loc"][1:]), "message": error["msg"]}
        for error in exc.errors()
    ]
    plural = len(errors) != 1
    return _without_caching(
        request,
        build_problem_response(
            status=422,
            title="Validation failed",
            detail=f"{len(errors)} fields are invalid" if plural else "1 field is invalid",
            problem_type="https://registry.thoriumreader.com/problems/validation",
            errors=errors,
            instance=build_problem_instance(request),
        ),
    )


async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
    """Anything not deliberately raised.

    Starlette's own handler answers `text/plain`, which breaks the contract that every error
    is a problem document. The detail is fixed, never `str(exc)`: an unexpected exception's
    message is the one most likely to carry a DSN, a file path, or a row of data, and this
    endpoint is public and unauthenticated. The request id in `X-Request-Id` is how the
    caller and the log are correlated instead.
    """
    return _without_caching(
        _request,
        build_problem_response(
            status=500,
            title="Internal Server Error",
            detail="An unexpected error occurred.",
            instance=build_problem_instance(_request),
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    configure_admin_log()
    app.add_exception_handler(RegistryError, handle_registry_error)
    app.add_exception_handler(HTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
