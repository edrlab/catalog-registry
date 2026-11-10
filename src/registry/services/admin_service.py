"""What the admin API says, assembled from what the repositories read."""

import dataclasses
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any

from registry.core.errors import (
    ArchivedReadOnlyError,
    CatalogNotFoundError,
    CountryNotFoundError,
    FieldValidationError,
    InvalidTransitionError,
    NotDeletableError,
    NotPublishedError,
    PermissionDeniedError,
    VersionConflictError,
)
from registry.core.schema_validation import build_catalog_validator
from registry.domain.admin_models import (
    AdminActor,
    AdminCatalogQuery,
    AdminCatalogRow,
    AdminCatalogState,
)
from registry.domain.catalog_document import (
    DocumentProblem,
    build_catalog_id,
    build_consistency_problems,
    build_identity_href,
    build_iso_code_problems,
)
from registry.domain.enums import CatalogKind, CatalogStatus
from registry.domain.labels import (
    COLOR_LABELS,
    COVERAGE_LABELS,
    KIND_LABELS,
    PUBLICATION_TYPE_LABELS,
    STATUS_LABELS,
)
from registry.domain.language import normalise_language_tag
from registry.domain.lifecycle import TRANSITIONS, UNDO_ACTIONS, UndoAction, build_allowed_actions
from registry.domain.permissions import build_permissions
from registry.repositories.protocols import ActorStore, AdminReader, AdminWriter, ProfileStore
from registry.schemas.admin import (
    CatalogDetail,
    CatalogListItem,
    CatalogListResponse,
    CountryDetail,
    CountryKpis,
    CountryListItem,
    CountryListResponse,
    CountryOption,
    CountryRef,
    FilterOptionsResponse,
    KindCounts,
    LanguageListResponse,
    LanguageOption,
    LanguageRef,
    LinkItem,
    MeResponse,
    Option,
    PlaceResponse,
    StatsResponse,
    StatusCounts,
    SubdivisionRef,
    SubdivisionTypeCount,
    SubdivisionTypeOption,
    Submitter,
)
from registry.services.admin_identity import AdminIdentity
from registry.services.language_names import build_language_list, build_language_name

#: Who may see a submitter's name and email: the roles that review submissions by publishing them.
SUBMITTER_PERMISSION = "catalog.publish"

#: ISO 3166-1 alpha-2, either case.
_COUNTRY_CODE = re.compile(r"[A-Za-z]{2}")

#: A direct public link exists for the statuses the public API serves by id.
_DIRECT_LINK_STATUSES = frozenset({CatalogStatus.PUBLISHED, CatalogStatus.UNPUBLISHED})


async def resolve_admin_actor(store: ActorStore, identity: AdminIdentity) -> AdminActor:
    """The person behind a verified identity: found by lowercased email, or created as a viewer on
    their first visit. A deactivated person is refused here, before any permission is looked at."""
    actor = await store.persist_actor_login(
        email=identity.email,
        name=identity.name,
        issuer=identity.issuer,
        subject=identity.subject,
    )
    if not actor.is_active:
        raise PermissionDeniedError("Your access has been removed.")
    return actor


def resolve_admin_me(actor: AdminActor) -> MeResponse:
    return MeResponse(
        id=actor.id,
        email=actor.email,
        name=actor.name or actor.email.partition("@")[0],
        name_is_set=bool(actor.name),
        role=actor.role.value,
        permissions=sorted(build_permissions(actor.role)),
        is_active=actor.is_active,
        created_at=actor.created_at,
        last_seen_at=actor.previous_seen_at,
        identity_issuer=actor.identity_issuer,
        identity_subject=actor.identity_subject,
    )


async def apply_admin_name(store: ProfileStore, actor: AdminActor, name: str | None) -> MeResponse:
    """Set or clear the caller's own display name. Only the name is self-editable."""
    chosen = name or None
    await store.persist_actor_name(actor.id, chosen)
    return resolve_admin_me(dataclasses.replace(actor, name=chosen))


async def resolve_admin_stats(reader: AdminReader) -> StatsResponse:
    stats = await reader.fetch_admin_stats()
    return StatsResponse(by_status=StatusCounts(**stats.by_status), recommended=stats.recommended)


async def resolve_admin_catalog_list(
    reader: AdminReader, actor: AdminActor, query: AdminCatalogQuery, *, base_url: str
) -> CatalogListResponse:
    page = await reader.fetch_admin_catalogs(query)
    permissions = build_permissions(actor.role)
    return CatalogListResponse(
        items=[_build_list_item(row, permissions, base_url) for row in page.rows],
        total=page.total,
        page=query.page,
        per_page=query.per_page,
    )


def _build_list_item(
    row: AdminCatalogRow, permissions: frozenset[str], base_url: str
) -> CatalogListItem:
    return CatalogListItem(**_list_item_fields(row, permissions, base_url))


def _list_item_fields(
    row: AdminCatalogRow, permissions: frozenset[str], base_url: str
) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "host": row.host,
        "color": row.color,
        "icon_url": row.icon_url,
        "kinds": list(row.kinds),
        "coverage": row.coverage,
        "place": PlaceResponse(
            city=row.city,
            subdivision=row.subdivision,
            country=(
                CountryRef(code=row.country_code, name=row.country_name)
                if row.country_code
                else None
            ),
        ),
        "languages": [LanguageRef(tag=tag, name=build_language_name(tag)) for tag in row.languages],
        "status": row.status,
        "recommended": row.recommended,
        "version": row.version,
        "allowed_actions": build_allowed_actions(row.status, permissions),
        "public_url": (
            f"{base_url.rstrip('/')}/catalogs/{row.id}"
            if row.status in _DIRECT_LINK_STATUSES
            else None
        ),
    }


def _normalise_catalog_id(catalog_id: str) -> str:
    try:
        return str(uuid.UUID(catalog_id))
    except ValueError:
        raise CatalogNotFoundError(f"No catalog with id {catalog_id}.") from None


def _require_catalog_permission(actor: AdminActor, action: str) -> None:
    permission = f"catalog.{action}"
    if permission not in build_permissions(actor.role):
        raise PermissionDeniedError(f"Your role cannot do this ({permission}).")


async def _lock_seen_catalog(
    writer: AdminWriter, catalog_id: str, version: int
) -> AdminCatalogState:
    """The catalog, locked for this write, as long as the caller saw its current version."""
    state = await writer.lock_admin_catalog(catalog_id)
    if state is None:
        raise CatalogNotFoundError(f"No catalog with id {catalog_id}.")
    if state.version != version:
        raise VersionConflictError(
            f"The catalog is at version {state.version}, not {version}: it changed since."
        )
    return state


async def apply_admin_transition(
    writer: AdminWriter, actor: AdminActor, catalog_id: str, *, action: str, version: int
) -> tuple[str, UndoAction | None]:
    """Move a catalog through the lifecycle. Returns its id and the action that undoes this one.

    Role, then version, then lifecycle: a role that may not act learns nothing about the catalog,
    and a stale version is reported before a transition that may only be refused because of it.
    """
    _require_catalog_permission(actor, action)
    normalised = _normalise_catalog_id(catalog_id)
    state = await _lock_seen_catalog(writer, normalised, version)
    target = TRANSITIONS.get((state.status, action))
    if target is None:
        raise InvalidTransitionError(
            f"A {state.status.value} catalog cannot take the action {action}."
        )
    await writer.persist_catalog_status(normalised, target)
    await writer.commit_admin_write()
    return normalised, UNDO_ACTIONS.get((state.status, action))


async def apply_admin_recommendation(
    writer: AdminWriter, actor: AdminActor, catalog_id: str, *, recommended: bool, version: int
) -> str:
    """Turn recommendation on or off. Published catalogs only; the flag is kept through
    unpublish and archive, but it is changed only while readers can see the catalog."""
    _require_catalog_permission(actor, "recommend")
    normalised = _normalise_catalog_id(catalog_id)
    state = await _lock_seen_catalog(writer, normalised, version)
    if state.status is not CatalogStatus.PUBLISHED:
        raise NotPublishedError(
            f"Only a published catalog can be recommended; this one is {state.status.value}."
        )
    await writer.persist_catalog_recommended(normalised, recommended)
    await writer.commit_admin_write()
    return normalised


#: Where each body field lives in the catalog document the seed reads.
_DOCUMENT_PATHS: Mapping[str, tuple[str, ...]] = {
    "name": ("metadata", "title"),
    "description": ("metadata", "description"),
    "color": ("metadata", "color"),
    "kinds": ("metadata", "kind"),
    "coverage": ("metadata", "coverage"),
    "country": ("metadata", "country"),
    "city": ("metadata", "city"),
    "subdivisions": ("metadata", "subdivisions"),
    "languages": ("metadata", "supportedLanguages"),
    "publication_types": ("metadata", "publicationTypes"),
    "links": ("links",),
}
_BODY_FIELDS = {path: field for field, path in _DOCUMENT_PATHS.items()}


def _apply_fields(document: dict[str, Any], fields: Mapping[str, Any]) -> dict[str, Any]:
    """*document* with *fields* (body names, JSON values) written in; `None` removes a field."""
    result: dict[str, Any] = {
        "metadata": dict(document["metadata"]),
        "links": list(document["links"]),
    }
    for field, value in fields.items():
        *parents, key = _DOCUMENT_PATHS[field]
        container: dict[str, Any] = result["metadata"] if parents else result
        if value is None:
            container.pop(key, None)
        elif field == "links":
            container[key] = [
                {name: item for name, item in link.items() if item not in (None, False)}
                for link in value
            ]
        else:
            container[key] = value
    return result


def _comparable(document: dict[str, Any]) -> tuple[Any, ...]:
    """*document* with list order and language case ignored: an edit form sends every field back,
    in its own order, and a save that changed nothing must keep the version."""
    metadata = {
        key: sorted(normalise_language_tag(tag) for tag in value)
        if key == "supportedLanguages"
        else sorted(value)
        if isinstance(value, list)
        else value
        for key, value in document["metadata"].items()
    }
    links = sorted(json.dumps(link, sort_keys=True) for link in document["links"])
    return json.dumps(metadata, sort_keys=True), links


def _body_path(path: tuple[str | int, ...]) -> str:
    """A document path as the body field it came from: `metadata.kind.0` is `kinds.0`."""
    if path[:1] == ("metadata",) and len(path) > 1:
        field = _BODY_FIELDS.get(("metadata", str(path[1])), str(path[1]))
        return ".".join([field, *(str(part) for part in path[2:])])
    return ".".join(str(part) for part in path)


#: A sentence for the schema checks whose own message quotes a regex or a whole list.
_SCHEMA_MESSAGES: Mapping[str, str] = {
    "pattern": "{!r} is not in the expected format",
    "uniqueItems": "The same item is listed twice",
}


def _validate_catalog_document(document: dict[str, Any]) -> None:
    """The seed's checks on one catalog, reported field by field. Raises `FieldValidationError`."""
    # The ISO check first: its message says what the code should be, where the schema's only
    # says it does not match. It runs safely on any metadata.
    iso = build_iso_code_problems(document["metadata"])
    problems = [*iso] + [
        DocumentProblem(
            tuple(error.absolute_path),
            _SCHEMA_MESSAGES[error.validator].format(error.instance)
            if error.validator in _SCHEMA_MESSAGES
            else error.message,
        )
        for error in build_catalog_validator().iter_errors(document)
        if tuple(error.absolute_path) not in {problem.path for problem in iso}
    ]
    if not problems:
        problems = build_consistency_problems(document)
    if problems:
        raise FieldValidationError(
            [{"field": _body_path(p.path), "message": p.message} for p in problems]
        )


async def _validate_places(writer: AdminWriter, document: dict[str, Any]) -> None:
    """Country and subdivisions must be codes the registry knows (both are foreign keys), and
    every subdivision must lie in the catalog's country, so one is needed when any is listed."""
    metadata = document["metadata"]
    country = metadata.get("country")
    subdivisions = metadata.get("subdivisions") or []
    country_known, unknown, foreign = await writer.fetch_unknown_places(country, subdivisions)
    errors = [] if country_known else [{"field": "country", "message": "Not a known country"}]
    if country is None and subdivisions:
        errors.append(
            {"field": "country", "message": "Choose the country of these subdivisions first"}
        )
    for index, code in enumerate(subdivisions):
        if code in unknown:
            errors.append(
                {"field": f"subdivisions.{index}", "message": f"{code} is not a known subdivision"}
            )
        elif code in foreign and country is not None and country_known:
            errors.append(
                {
                    "field": f"subdivisions.{index}",
                    "message": f"{code} is not in this catalog's country",
                }
            )
    if errors:
        raise FieldValidationError(errors)


async def apply_admin_catalog_creation(
    writer: AdminWriter, actor: AdminActor, fields: Mapping[str, Any], *, publish: bool
) -> str:
    """A new catalog, as a draft or published, checked as the seed checks one. Its id is
    derived from its identity href, as the seed derives it. Returns the id."""
    _require_catalog_permission(actor, "create")
    if publish:
        _require_catalog_permission(actor, "publish")
    document = _apply_fields({"metadata": {}, "links": []}, fields)
    _validate_catalog_document(document)
    await _validate_places(writer, document)
    identity_href = build_identity_href(document)
    assert identity_href is not None  # `build_consistency_problems` refused a document without one
    catalog_id = build_catalog_id(identity_href)
    await writer.persist_new_catalog(document, catalog_id, publish=publish)
    await writer.commit_admin_write()
    return str(catalog_id)


async def apply_admin_catalog_edit(
    writer: AdminWriter,
    actor: AdminActor,
    catalog_id: str,
    *,
    version: int,
    changes: Mapping[str, Any],
) -> str:
    """Change some fields of a catalog. The result is checked whole, as a new catalog would be.
    An edit that changes nothing keeps the version. Archived catalogs are read-only."""
    _require_catalog_permission(actor, "edit")
    normalised = _normalise_catalog_id(catalog_id)
    state = await _lock_seen_catalog(writer, normalised, version)
    if state.status is CatalogStatus.ARCHIVED:
        raise ArchivedReadOnlyError("An archived catalog cannot be edited; unarchive it first.")
    current = await writer.fetch_catalog_document(normalised)
    document = _apply_fields(current, changes)
    if _comparable(document) == _comparable(current):
        return normalised
    _validate_catalog_document(document)
    await _validate_places(writer, document)
    await writer.persist_catalog_document(normalised, document)
    await writer.commit_admin_write()
    return normalised


async def apply_admin_catalog_deletion(
    writer: AdminWriter, actor: AdminActor, catalog_id: str, *, version: int
) -> None:
    """Delete a draft for good. Anything else was, or could have been, seen by readers, so it is
    archived instead (409 `not-deletable`)."""
    _require_catalog_permission(actor, "delete")
    normalised = _normalise_catalog_id(catalog_id)
    state = await _lock_seen_catalog(writer, normalised, version)
    if state.status is not CatalogStatus.DRAFT:
        raise NotDeletableError(
            f"Only a draft can be deleted; this one is {state.status.value}. Archive it instead."
        )
    await writer.persist_catalog_deletion(normalised)
    await writer.commit_admin_write()


async def resolve_admin_catalog_detail(
    reader: AdminReader, actor: AdminActor, catalog_id: str, *, base_url: str
) -> CatalogDetail:
    """One catalog in any status. An id that is not a UUID is simply not found: it comes from a
    page URL, where a hand-edited link should read as "no such catalog"."""
    normalised = _normalise_catalog_id(catalog_id)
    detail = await reader.fetch_admin_catalog(normalised)
    if detail is None:
        raise CatalogNotFoundError(f"No catalog with id {normalised}.")
    permissions = build_permissions(actor.role)
    return CatalogDetail(
        **_list_item_fields(detail.row, permissions, base_url),
        description=detail.description,
        publication_types=list(detail.publication_types),
        subdivisions=[SubdivisionRef(code=s.code, name=s.name) for s in detail.subdivisions],
        links=[
            LinkItem(
                rel=link.rel,
                href=link.href,
                type=link.media_type,
                title=link.title,
                templated=link.templated,
                authentication=link.authentication,
            )
            for link in detail.links
        ],
        created_at=detail.created_at,
        updated_at=detail.updated_at,
        published_at=detail.published_at,
        submitter=(
            Submitter(name=detail.submitter_name, email=detail.submitter_email)
            if SUBMITTER_PERMISSION in permissions
            and (detail.submitter_name or detail.submitter_email)
            else None
        ),
    )


def _build_options(labels: Mapping[str, str]) -> list[Option]:
    return [Option(value=value, label=label) for value, label in labels.items()]


def resolve_admin_language_list() -> LanguageListResponse:
    return LanguageListResponse(
        items=[LanguageRef(tag=tag, name=name) for tag, name in build_language_list()]
    )


async def resolve_filter_options(reader: AdminReader) -> FilterOptionsResponse:
    places = await reader.fetch_used_places()
    languages = sorted(
        (
            LanguageOption(
                tag=language.tag,
                name=build_language_name(language.tag),
                catalog_count=language.catalog_count,
            )
            for language in places.languages
        ),
        key=lambda option: option.name.casefold(),
    )
    return FilterOptionsResponse(
        statuses=_build_options(STATUS_LABELS),
        kinds=_build_options(KIND_LABELS),
        coverages=_build_options(COVERAGE_LABELS),
        publication_types=_build_options(PUBLICATION_TYPE_LABELS),
        colors=_build_options(COLOR_LABELS),
        countries=sorted(
            (
                CountryOption(code=c.code, name=c.name, catalog_count=c.catalog_count)
                for c in places.countries
            ),
            key=lambda option: option.name.casefold(),
        ),
        languages=languages,
    )


async def resolve_admin_country_list(
    reader: AdminReader, q: str | None, page: int, per_page: int
) -> CountryListResponse:
    result = await reader.fetch_admin_countries(q, page, per_page)
    return CountryListResponse(
        items=[
            CountryListItem(
                code=row.code, name=row.name, published_catalog_count=row.published_catalog_count
            )
            for row in result.rows
        ],
        total=result.total,
        page=page,
        per_page=per_page,
    )


async def apply_country_subdivision_types(
    writer: AdminWriter, actor: AdminActor, code: str, keys: list[str]
) -> str:
    """Replace the types *code* offers with *keys*. Each must be one the country
    may offer; `[]` offers none. Catalogs keep the subdivisions they use. Returns the code."""
    if "country.manage_subdivisions" not in build_permissions(actor.role):
        raise PermissionDeniedError("Your role cannot do this (country.manage_subdivisions).")
    if not _COUNTRY_CODE.fullmatch(code):
        raise CountryNotFoundError(f"No country with code {code}.")
    normalised = code.upper()
    available = await writer.lock_country_types(normalised)
    if available is None:
        raise CountryNotFoundError(f"No country with code {normalised}.")
    errors = []
    for index, key in enumerate(keys):
        if key in keys[:index]:
            errors.append({"field": f"keys.{index}", "message": f"{key!r} is listed twice"})
        elif key not in available:
            errors.append(
                {"field": f"keys.{index}", "message": f"{key!r} is not a type this country has"}
            )
    if errors:
        raise FieldValidationError(errors)
    await writer.persist_country_types(normalised, keys)
    await writer.commit_admin_write()
    return normalised


def _label_subdivision_type(key: str) -> str:
    """No label table: the key is ISO 3166-2's own type name, lowercased."""
    return key[:1].upper() + key[1:]


async def resolve_admin_country_detail(reader: AdminReader, code: str) -> CountryDetail:
    """One country by ISO 3166-1 alpha-2 code, either case. Anything else is simply not found, as a
    malformed catalog id is."""
    if not _COUNTRY_CODE.fullmatch(code):
        raise CountryNotFoundError(f"No country with code {code}.")
    detail = await reader.fetch_admin_country(code.upper())
    if detail is None:
        raise CountryNotFoundError(f"No country with code {code.upper()}.")
    return CountryDetail(
        code=detail.code,
        name=detail.name,
        official_languages=sorted(
            (LanguageRef(tag=tag, name=build_language_name(tag)) for tag in detail.language_tags),
            key=lambda language: language.name.casefold(),
        ),
        subdivision_types=[
            SubdivisionTypeCount(
                key=key,
                label=_label_subdivision_type(key),
                subdivision_count=len(subdivisions),
                subdivisions=[SubdivisionRef(code=s.code, name=s.name) for s in subdivisions],
            )
            for key, subdivisions in detail.subdivision_types
        ],
        available_subdivision_types=[
            SubdivisionTypeOption(key=key, label=_label_subdivision_type(key))
            for key in detail.available_subdivision_types
        ],
        kpis=CountryKpis(
            by_status=StatusCounts(**detail.by_status),
            recommended=detail.recommended,
            by_kind=KindCounts(
                **{kind.value: detail.by_kind.get(kind.value, 0) for kind in CatalogKind}
            ),
        ),
    )
