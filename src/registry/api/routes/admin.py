"""The admin API: `/admin/*`. Server to server only; see `api/admin_dependencies.py`.

Every route depends on `require_permission(...)`. A write also checks the permission for its
action in the service, where the action is known.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import StringConstraints

from registry.api.admin_dependencies import require_permission
from registry.api.admin_log import ICON_PROBLEM_STATE_KEY
from registry.core.errors import (
    AdminUnavailableError,
    ChecksBusyError,
    FieldValidationError,
    IconTooLargeError,
    RegistryError,
)
from registry.domain.admin_models import (
    MAX_PAGE,
    MAX_PER_PAGE,
    MAX_QUERY_WORDS,
    AdminActor,
    AdminCatalogQuery,
)
from registry.domain.enums import (
    ActorRole,
    CatalogKind,
    CatalogStatus,
    CoverageScope,
    PublicationType,
)
from registry.domain.fetching import FetchError
from registry.network.icon_worker import MAX_ICON_BYTES
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
    LanguageListResponse,
    MeResponse,
    MeUpdateRequest,
    ProblemDocument,
    RecommendedRequest,
    StatsResponse,
    SubdivisionTypesRequest,
    TransitionRequest,
    TransitionResponse,
    UserItem,
    UserListResponse,
    UserRoleRequest,
)
from registry.services.admin_service import (
    apply_admin_catalog_creation,
    apply_admin_catalog_deletion,
    apply_admin_catalog_edit,
    apply_admin_catalog_icon,
    apply_admin_catalog_remote_icon,
    apply_admin_name,
    apply_admin_recommendation,
    apply_admin_transition,
    apply_country_subdivision_types,
    describe_icon_failure,
    resolve_admin_catalog_detail,
    resolve_admin_catalog_list,
    resolve_admin_country_detail,
    resolve_admin_country_list,
    resolve_admin_language_list,
    resolve_admin_me,
    resolve_admin_stats,
    resolve_filter_options,
)
from registry.services.feed_check_service import resolve_feed_check
from registry.services.user_service import (
    apply_user_activation,
    apply_user_role_change,
    resolve_admin_users,
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

NOT_FOUND = "No catalog with this id, or the id is not a UUID (type `catalog-not-found`)."


def _is_stored_icon(request: Request, href: str | None) -> bool:
    """*href* is the registry's own stored icon, not a library's link."""
    base = request.app.state.settings.icon_base_url
    return bool(base and href and href.startswith(base.rstrip("/") + "/"))


async def _read_detail(request: Request, actor: AdminActor, catalog_id: str) -> CatalogDetail:
    async with request.app.state.open_admin_reader() as reader:
        detail = await resolve_admin_catalog_detail(
            reader, actor, catalog_id, base_url=request.app.state.settings.base_url
        )
    return detail.model_copy(update={"icon_stored": _is_stored_icon(request, detail.icon_url)})


async def _store_icon_from(
    request: Request, actor: AdminActor, detail: CatalogDetail, href: str
) -> CatalogDetail:
    """Store the library's icon at *href* as the catalog's own; the detail afterwards."""
    persist = request.app.state.persist_catalog_icon
    async with request.app.state.open_admin_writer() as writer:
        await apply_admin_catalog_remote_icon(
            writer,
            actor,
            detail.id,
            version=detail.version,
            href=href,
            fetch_icon=request.app.state.fetch_catalog_icon,
            store_icon=persist,
        )
    return await _read_detail(request, actor, detail.id)


async def _store_new_icon(
    request: Request, actor: AdminActor, detail: CatalogDetail, before: set[str]
) -> CatalogDetail:
    """After a save: an icon link it added that is not stored yet is stored, as the back office's
    upload would. When that fails the save stands, with the library's link and `icon_problem`."""
    if not request.app.state.settings.icon_base_url:
        return detail
    href = next(
        (
            link.href
            for link in detail.links
            if link.rel == "icon"
            and link.href not in before
            and not _is_stored_icon(request, link.href)
        ),
        None,
    )
    if href is None:
        return detail
    try:
        return await _store_icon_from(request, actor, detail, href)
    except (FetchError, RegistryError) as error:
        _note_icon_problem(request, error)
        return detail.model_copy(update={"icon_problem": describe_icon_failure(error)})


def _note_icon_problem(request: Request, error: Exception) -> None:
    """The technical reason, for the request's log line; the editor gets plain words."""
    reason = error.errors[0]["message"] if isinstance(error, FieldValidationError) else str(error)
    code = f"{error.code} " if isinstance(error, FetchError) else ""
    setattr(request.state, ICON_PROBLEM_STATE_KEY, f"{type(error).__name__}: {code}{reason}")


#: A catalog list that names no status shows what readers see.
DEFAULT_STATUSES = (CatalogStatus.PUBLISHED,)


@router.get("/me", summary="The caller and what their role may do")
async def read_admin_me(
    actor: Annotated[AdminActor, Depends(require_permission("backoffice.view"))],
) -> MeResponse:
    return resolve_admin_me(actor)


@router.patch(
    "/me",
    summary="Change your own display name",
    responses={422: problem_response("The name is invalid; `errors` names it.")},
)
async def update_admin_me(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("backoffice.view"))],
    body: MeUpdateRequest,
) -> MeResponse:
    async with request.app.state.open_actor_store() as store:
        return await apply_admin_name(store, actor, body.name)


@router.get("/stats", summary="Catalog counts by status")
async def read_admin_stats(
    request: Request, _actor: Annotated[AdminActor, Depends(require_permission("backoffice.view"))]
) -> StatsResponse:
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_admin_stats(reader)


#: A filter is a short list someone picked; a longer one only makes the query bigger.
MAX_FILTER_VALUES = 50


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
        list[Annotated[str, StringConstraints(pattern=r"^(?:[A-Za-z]{2}|none)$")]] | None,
        Query(
            max_length=MAX_FILTER_VALUES,
            description="ISO 3166-1 alpha-2 code, any case, or `none` for catalogs with no "
            "country. Repeat to match any of them: `country=none&country=FR`.",
        ),
    ] = None,
    language: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,35}$")]] | None,
        Query(max_length=MAX_FILTER_VALUES),
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
    return await _read_detail(request, actor, catalog_id)


@router.get("/filter-options", summary="Every enumeration with labels, and the places in use")
async def read_filter_options(
    request: Request, _actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))]
) -> FilterOptionsResponse:
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_filter_options(reader)


@router.get("/languages", summary="Every language the registry can name, by name")
async def read_admin_languages(
    _actor: Annotated[AdminActor, Depends(require_permission("backoffice.view"))],
) -> LanguageListResponse:
    return resolve_admin_language_list()


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
    catalog = await _read_detail(request, actor, normalised)
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
    return await _read_detail(request, actor, normalised)


INVALID_BODY = "The body is invalid; `errors` names each field."


@router.post(
    "/catalogs",
    status_code=201,
    summary="Add a catalog, as a draft or published",
    responses={
        409: problem_response(
            "`duplicate-catalog`: another catalog already has this catalog or shelf link, or was "
            "created from this address and now uses another one."
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
    return await _store_new_icon(
        request, actor, await _read_detail(request, actor, catalog_id), set()
    )


@router.patch(
    "/catalogs/{catalog_id}",
    summary="Change some fields of a catalog",
    responses={
        404: problem_response(NOT_FOUND),
        409: problem_response(
            "`version-conflict`: it changed since it was read. "
            "`archived-read-only`: an archived catalog cannot be edited. "
            "`duplicate-catalog`: another catalog already has this catalog or shelf link, or was "
            "created from this address and now uses another one."
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
    changes = body.model_dump(mode="json", exclude_unset=True, exclude={"version"})
    # Only an icon this edit adds is stored: an ordinary edit downloads nothing.
    before = (
        {link.href for link in (await _read_detail(request, actor, catalog_id)).links}
        if "links" in changes
        else None
    )
    async with request.app.state.open_admin_writer() as writer:
        normalised = await apply_admin_catalog_edit(
            writer, actor, catalog_id, version=body.version, changes=changes
        )
    detail = await _read_detail(request, actor, normalised)
    return detail if before is None else await _store_new_icon(request, actor, detail, before)


async def _read_icon(request: Request) -> tuple[bytes, str]:
    """The uploaded image and its media type, read up to `MAX_ICON_BYTES` and no further."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not media_type.startswith("image/"):
        raise FieldValidationError([{"field": "body", "message": "Send an image"}])
    image = bytearray()
    async for chunk in request.stream():
        image += chunk
        if len(image) > MAX_ICON_BYTES:
            raise IconTooLargeError("The image is over 5 MB.")
    if not image:
        raise FieldValidationError([{"field": "body", "message": "Send an image"}])
    return bytes(image), media_type


@router.put(
    "/catalogs/{catalog_id}/icon",
    summary="Replace a catalog's icon with an uploaded image, stored as a 512 px PNG",
    description=(
        "The body is the image itself (`Content-Type: image/png`, `image/jpeg`, `image/webp` or "
        "`image/gif`; SVG cannot be converted), at most 5 MB. The catalog's icon links are "
        "replaced by one link to the stored PNG."
    ),
    responses={
        404: problem_response(NOT_FOUND),
        409: problem_response(
            "`version-conflict`: it changed since it was read. "
            "`archived-read-only`: an archived catalog cannot be edited."
        ),
        413: problem_response("`icon-too-large`: the image is over 5 MB."),
        422: problem_response("Not an image, or not one the image service can read."),
        503: problem_response("Icon uploads are not configured, or the image service is down."),
    },
)
async def replace_admin_catalog_icon(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))],
    catalog_id: str,
    version: Annotated[int, Query(ge=1, description="The version the catalog was read at.")],
) -> CatalogDetail:
    image, media_type = await _read_icon(request)
    persist = request.app.state.persist_catalog_icon
    async with request.app.state.open_admin_writer() as writer:
        normalised = await apply_admin_catalog_icon(
            writer,
            actor,
            catalog_id,
            version=version,
            store_icon=lambda stored_id: persist(stored_id, image, media_type),
        )
    return await _read_detail(request, actor, normalised)


@router.post(
    "/catalogs/{catalog_id}/icon/store",
    summary="Store the catalog's icon link as the registry's own 512 px PNG",
    description=(
        "For an icon that could not be stored when it was saved (the library's site was down, "
        "say): the registry downloads it again and stores it, as an upload of it would."
    ),
    responses={
        404: problem_response(NOT_FOUND),
        409: problem_response(
            "`version-conflict`: it changed since it was read. "
            "`archived-read-only`: an archived catalog cannot be edited."
        ),
        422: problem_response(
            "`errors[0].field = icon`: no icon link to store, or it could not be downloaded or "
            "converted; the message says which."
        ),
        503: problem_response("Icon storage is not configured, or the image service is down."),
    },
)
async def store_admin_catalog_icon(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("catalog.view"))],
    catalog_id: str,
    version: Annotated[int, Query(ge=1, description="The version the catalog was read at.")],
) -> CatalogDetail:
    if not request.app.state.settings.icon_base_url:
        raise AdminUnavailableError("Icon uploads are not configured.")
    detail = await _read_detail(request, actor, catalog_id)
    href = next(
        (
            link.href
            for link in detail.links
            if link.rel == "icon" and not _is_stored_icon(request, link.href)
        ),
        None,
    )
    if href is None:
        raise FieldValidationError([{"field": "icon", "message": "There is no icon link to store"}])
    try:
        return await _store_icon_from(
            request, actor, detail.model_copy(update={"version": version}), href
        )
    except (FetchError, FieldValidationError) as error:
        _note_icon_problem(request, error)
        raise FieldValidationError(
            [{"field": "icon", "message": describe_icon_failure(error)}]
        ) from error


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
    """One synchronous check, nothing stored."""
    slots = request.app.state.feed_check_slots
    if slots.locked():
        raise ChecksBusyError("Too many feed checks are running. Try again in a few seconds.")
    async with (
        slots,
        request.app.state.open_admin_reader() as reader,
        request.app.state.open_feed_fetcher() as fetcher,
    ):
        return await resolve_feed_check(
            fetcher, reader, actor, body.url, catalog_id=body.catalog_id, website=body.website
        )


USER_NOT_FOUND = "No user with this id, or the id is not a UUID (type `user-not-found`)."
USER_CONFLICT = "`self-change`: not on yourself. `last-admin`: it would leave no active admin."


@router.get("/users", summary="Everyone with access to the back office, and what you may do")
async def read_admin_users(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("backoffice.view"))],
) -> UserListResponse:
    async with request.app.state.open_actor_store() as store:
        return await resolve_admin_users(store, actor)


@router.patch(
    "/users/{user_id}",
    summary="Change someone's role",
    responses={
        404: problem_response(USER_NOT_FOUND),
        409: problem_response(USER_CONFLICT),
        422: problem_response(INVALID_BODY),
    },
)
async def update_admin_user(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("user.change_role"))],
    user_id: str,
    body: UserRoleRequest,
) -> UserItem:
    async with request.app.state.open_actor_store() as store:
        return await apply_user_role_change(store, actor, user_id, ActorRole(body.role))


@router.post(
    "/users/{user_id}/deactivate",
    summary="Remove someone's access",
    responses={404: problem_response(USER_NOT_FOUND), 409: problem_response(USER_CONFLICT)},
)
async def create_admin_user_deactivation(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("user.deactivate"))],
    user_id: str,
) -> UserItem:
    async with request.app.state.open_actor_store() as store:
        return await apply_user_activation(store, actor, user_id, active=False)


@router.post(
    "/users/{user_id}/reactivate",
    summary="Give someone their access back",
    responses={404: problem_response(USER_NOT_FOUND), 409: problem_response(USER_CONFLICT)},
)
async def create_admin_user_reactivation(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("user.deactivate"))],
    user_id: str,
) -> UserItem:
    async with request.app.state.open_actor_store() as store:
        return await apply_user_activation(store, actor, user_id, active=True)


@router.put(
    "/countries/{code}/subdivision-types",
    summary="Choose which subdivision types a country offers",
    responses={
        404: problem_response("No country with this code (type `country-not-found`)."),
        422: problem_response("A key is not one of the country's types; `errors` names it."),
    },
)
async def replace_admin_country_subdivision_types(
    request: Request,
    actor: Annotated[AdminActor, Depends(require_permission("country.manage_subdivisions"))],
    code: str,
    body: SubdivisionTypesRequest,
) -> CountryDetail:
    async with request.app.state.open_admin_writer() as writer:
        normalised = await apply_country_subdivision_types(writer, actor, code, body.keys)
    async with request.app.state.open_admin_reader() as reader:
        return await resolve_admin_country_detail(reader, normalised)
