"""The admin API: `/admin/*`. Server to server only; see `api/admin_dependencies.py`.

Read-only for now. Every route depends on `require_permission(...)`.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import StringConstraints

from registry.api.admin_dependencies import require_permission
from registry.domain.admin_models import (
    MAX_PAGE,
    MAX_PER_PAGE,
    MAX_QUERY_WORDS,
    AdminActor,
    AdminCatalogQuery,
)
from registry.domain.enums import CatalogKind, CatalogStatus, CoverageScope, PublicationType
from registry.schemas.admin import (
    CatalogDetail,
    CatalogListResponse,
    FilterOptionsResponse,
    MeResponse,
    ProblemDocument,
    StatsResponse,
)
from registry.services.admin_service import (
    resolve_admin_catalog_detail,
    resolve_admin_catalog_list,
    resolve_admin_me,
    resolve_admin_stats,
    resolve_filter_options,
)

PROBLEM_CONTENT = {
    "application/problem+json": {"schema": {"$ref": "#/components/schemas/ProblemDocument"}}
}


def problem_response(description: str) -> dict[str, object]:
    return {"model": ProblemDocument, "description": description, "content": PROBLEM_CONTENT}


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    responses={
        401: problem_response("No or wrong key, or no valid token."),
        403: problem_response(
            "Outside the allowed domains, deactivated, or the role cannot do this."
        ),
        500: problem_response("Unexpected. The detail is fixed; find it in the log by `instance`."),
        503: problem_response("Not configured, or the signing keys cannot be fetched."),
    },
)

#: A catalog list that names no status shows what readers see.
DEFAULT_STATUSES = (CatalogStatus.PUBLISHED,)


@router.get("/me", summary="The caller and what their role may do")
async def read_admin_me(
    actor: Annotated[AdminActor, Depends(require_permission("backoffice.view"))],
) -> MeResponse:
    return resolve_admin_me(actor)


@router.get("/stats", summary="Catalog counts by status")
async def read_admin_stats(
    request: Request, _actor: Annotated[AdminActor, Depends(require_permission("backoffice.view"))]
) -> StatsResponse:
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_admin_stats(reader)


@router.get(
    "/catalogs",
    summary="Catalogs, filtered, with what each row needs",
    responses={422: problem_response("A query parameter is invalid; `errors` names each field.")},
)
async def read_admin_catalogs(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))],
    *,
    q: Annotated[str | None, Query(max_length=200, pattern=r"^[^\x00]*$")] = None,
    status: Annotated[list[CatalogStatus] | None, Query()] = None,
    kind: Annotated[list[CatalogKind] | None, Query()] = None,
    coverage: Annotated[list[CoverageScope] | None, Query()] = None,
    publication_type: Annotated[list[PublicationType] | None, Query()] = None,
    country: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{2}$")]] | None, Query()
    ] = None,
    language: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,35}$")]] | None, Query()
    ] = None,
    recommended: bool | None = None,
    sort: Literal["name", "-name", "kind", "status"] = "name",
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=MAX_PER_PAGE)] = MAX_PER_PAGE,
) -> CatalogListResponse:
    query = AdminCatalogQuery(
        words=(q or "").split()[:MAX_QUERY_WORDS],
        statuses=status or DEFAULT_STATUSES,
        kinds=[value.value for value in kind or ()],
        coverages=[value.value for value in coverage or ()],
        publication_types=[value.value for value in publication_type or ()],
        countries=[value.upper() for value in country or ()],
        languages=[value.lower() for value in language or ()],
        recommended=recommended,
        sort=sort,
        page=page,
        per_page=per_page,
    )
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_admin_catalog_list(
            reader, actor, query, base_url=request.app.state.settings.base_url
        )


@router.get(
    "/catalogs/{catalog_id}",
    summary="One catalog in any status, with everything the detail page shows",
    responses={404: problem_response("No catalog with this id, or the id is not a UUID.")},
)
async def read_admin_catalog(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))],
    catalog_id: str,
) -> CatalogDetail:
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_admin_catalog_detail(
            reader, actor, catalog_id, base_url=request.app.state.settings.base_url
        )


@router.get("/filter-options", summary="Every enumeration with labels, and the places in use")
async def read_filter_options(
    request: Request, _actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))]
) -> FilterOptionsResponse:
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_filter_options(reader)
