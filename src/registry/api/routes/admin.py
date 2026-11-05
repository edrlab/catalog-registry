"""The admin API: `/admin/*`. Server to server only; see `api/admin_dependencies.py`.

Every route depends on `require_permission(...)`. A write also checks the permission for its
action in the service, where the action is known.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import StringConstraints

from registry.api.admin_dependencies import require_permission
from registry.core.errors import ChecksBusyError
from registry.domain.admin_models import (
    MAX_PAGE,
    MAX_PER_PAGE,
    MAX_QUERY_WORDS,
    AdminActor,
    AdminCatalogQuery,
)
from registry.domain.enums import CatalogKind, CatalogStatus, CoverageScope, PublicationType
from registry.schemas.admin import (
    CatalogCreateRequest,
    CatalogDetail,
    CatalogListResponse,
    CatalogUpdateRequest,
    CountryDetail,
    CountryListResponse,
    FeedCheckRequest,
    FeedCheckResponse,
    FilterOptionsResponse,
    MeResponse,
    ProblemDocument,
    RecommendedRequest,
    StatsResponse,
    TransitionRequest,
    TransitionResponse,
)
from registry.services.admin_service import (
    apply_admin_catalog_creation,
    apply_admin_catalog_deletion,
    apply_admin_catalog_edit,
    apply_admin_recommendation,
    apply_admin_transition,
    resolve_admin_catalog_detail,
    resolve_admin_catalog_list,
    resolve_admin_country_detail,
    resolve_admin_country_list,
    resolve_admin_me,
    resolve_admin_stats,
    resolve_filter_options,
)
from registry.services.feed_check_service import resolve_feed_check

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

NOT_FOUND = "No catalog with this id, or the id is not a UUID (type `catalog-not-found`)."

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
    responses={404: problem_response(NOT_FOUND)},
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


@router.get(
    "/countries",
    summary="Countries with at least one catalog, most published first",
    responses={422: problem_response("A query parameter is invalid; `errors` names each field.")},
)
async def read_admin_countries(
    request: Request,
    _actor: Annotated[AdminActor, Depends(require_permission("country.view"))],
    *,
    q: Annotated[str | None, Query(max_length=200, pattern=r"^[^\x00]*$")] = None,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=MAX_PER_PAGE)] = MAX_PER_PAGE,
) -> CountryListResponse:
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_admin_country_list(
            reader, (q or "").strip() or None, page=page, per_page=per_page
        )


@router.get(
    "/countries/{code}",
    summary="One country: its languages, subdivision types and catalog counts",
    responses={404: problem_response("No country with this code (type `country-not-found`).")},
)
async def read_admin_country(
    request: Request,
    _actor: Annotated[AdminActor, Depends(require_permission("country.view"))],
    code: str,
) -> CountryDetail:
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_admin_country_detail(reader, code)


@router.post(
    "/catalogs/{catalog_id}/transitions",
    summary="Publish, unpublish, archive or unarchive a catalog",
    responses={
        404: problem_response(NOT_FOUND),
        409: problem_response(
            "`version-conflict`: it changed since it was read. "
            "`invalid-transition`: the lifecycle does not allow this action from its status."
        ),
        422: problem_response("The body is invalid; `errors` names each field."),
    },
)
async def create_admin_catalog_transition(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))],
    catalog_id: str,
    body: TransitionRequest,
) -> TransitionResponse:
    async with request.app.state.open_admin_writer() as writer:
        normalised, undo_action = await apply_admin_transition(
            writer, actor, catalog_id, action=body.action, version=body.version
        )
    async with request.app.state.open_admin_reader() as reader:
        catalog = await resolve_admin_catalog_detail(
            reader, actor, normalised, base_url=request.app.state.settings.base_url
        )
    return TransitionResponse(catalog=catalog, undo_action=undo_action)


@router.put(
    "/catalogs/{catalog_id}/recommended",
    summary="Recommend a published catalog, or stop recommending it",
    responses={
        404: problem_response(NOT_FOUND),
        409: problem_response(
            "`version-conflict`: it changed since it was read. "
            "`not-published`: only a published catalog can change its recommendation."
        ),
        422: problem_response("The body is invalid; `errors` names each field."),
    },
)
async def replace_admin_catalog_recommended(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))],
    catalog_id: str,
    body: RecommendedRequest,
) -> CatalogDetail:
    async with request.app.state.open_admin_writer() as writer:
        normalised = await apply_admin_recommendation(
            writer, actor, catalog_id, recommended=body.recommended, version=body.version
        )
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_admin_catalog_detail(
            reader, actor, normalised, base_url=request.app.state.settings.base_url
        )


INVALID_BODY = "The body is invalid; `errors` names each field."


@router.post(
    "/catalogs",
    status_code=201,
    summary="Add a catalog, as a draft or published",
    responses={
        409: problem_response(
            "`duplicate-catalog`: another catalog already has this catalog or shelf link."
        ),
        422: problem_response(INVALID_BODY),
    },
)
async def create_admin_catalog(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))],
    body: CatalogCreateRequest,
) -> CatalogDetail:
    async with request.app.state.open_admin_writer() as writer:
        catalog_id = await apply_admin_catalog_creation(
            writer,
            actor,
            body.model_dump(mode="json", exclude={"publish"}),
            publish=body.publish,
        )
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_admin_catalog_detail(
            reader, actor, catalog_id, base_url=request.app.state.settings.base_url
        )


@router.patch(
    "/catalogs/{catalog_id}",
    summary="Change some fields of a catalog",
    responses={
        404: problem_response(NOT_FOUND),
        409: problem_response(
            "`version-conflict`: it changed since it was read. "
            "`archived-read-only`: an archived catalog cannot be edited. "
            "`duplicate-catalog`: another catalog already has this catalog or shelf link."
        ),
        422: problem_response(INVALID_BODY),
    },
)
async def update_admin_catalog(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))],
    catalog_id: str,
    body: CatalogUpdateRequest,
) -> CatalogDetail:
    async with request.app.state.open_admin_writer() as writer:
        normalised = await apply_admin_catalog_edit(
            writer,
            actor,
            catalog_id,
            version=body.version,
            changes=body.model_dump(mode="json", exclude_unset=True, exclude={"version"}),
        )
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_admin_catalog_detail(
            reader, actor, normalised, base_url=request.app.state.settings.base_url
        )


@router.delete(
    "/catalogs/{catalog_id}",
    status_code=204,
    summary="Delete a draft",
    responses={
        404: problem_response(NOT_FOUND),
        409: problem_response(
            "`version-conflict`: it changed since it was read. "
            "`not-deletable`: only a draft can be deleted."
        ),
        422: problem_response("`version` is missing or invalid."),
    },
)
async def delete_admin_catalog(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))],
    catalog_id: str,
    # A query string is text, so not `SeenVersion`, whose strictness is for JSON bodies.
    version: Annotated[int, Query(ge=1)],
) -> Response:
    async with request.app.state.open_admin_writer() as writer:
        await apply_admin_catalog_deletion(writer, actor, catalog_id, version=version)
    return Response(status_code=204)


@router.post(
    "/feed-checks",
    summary="Check a feed or web page address and say what a catalog would get from it",
    responses={
        404: problem_response(NOT_FOUND),
        422: problem_response(INVALID_BODY),
        429: problem_response(
            "`checks-busy`: the maximum of checks is running; try again after `Retry-After`."
        ),
    },
)
async def create_admin_feed_check(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))],
    body: FeedCheckRequest,
) -> FeedCheckResponse:
    """One synchronous check, nothing stored (contract 0.8, ADR-066)."""
    slots = request.app.state.feed_check_slots
    if slots.locked():
        raise ChecksBusyError("Too many feed checks are running. Try again in a few seconds.")
    async with (
        slots,
        request.app.state.open_admin_reader() as reader,
        request.app.state.open_feed_fetcher() as fetcher,
    ):
        return await resolve_feed_check(
            fetcher, reader, actor, body.url, catalog_id=body.catalog_id
        )
