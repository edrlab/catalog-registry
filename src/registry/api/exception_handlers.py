"""Domain errors and framework errors, rendered as RFC 9457 problem documents."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from registry.core.constants import PROBLEM_JSON_MEDIA_TYPE
from registry.core.errors import RegistryError


def build_problem_response(*, status: int, title: str, detail: str) -> JSONResponse:
    """RFC 9457 §3 problem details."""
    return JSONResponse(
        status_code=status,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


async def handle_registry_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RegistryError):
        raise exc
    return build_problem_response(status=exc.status_code, title=exc.title, detail=str(exc))


async def handle_http_exception(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        raise exc
    return build_problem_response(
        status=exc.status_code, title="HTTP Error", detail=str(exc.detail)
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RegistryError, handle_registry_error)
    app.add_exception_handler(HTTPException, handle_http_exception)
