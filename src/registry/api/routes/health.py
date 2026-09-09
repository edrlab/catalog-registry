"""Liveness and readiness.

Liveness must not depend on the database: a probe that fails when Postgres is down gets the
container restarted, which fixes nothing.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def read_liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready")
async def read_readiness(request: Request) -> JSONResponse:
    try:
        await request.app.state.check_database_connection()
    except Exception:  # any failure to reach the database means not ready
        return JSONResponse(status_code=503, content={"status": "not ready"})
    return JSONResponse(status_code=200, content={"status": "ready"})
