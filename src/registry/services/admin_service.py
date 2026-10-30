"""What the admin API says, assembled from what the repositories read."""

import uuid
from collections.abc import Mapping
from typing import Any

from registry.core.errors import NotFoundError, PermissionDeniedError
from registry.domain.admin_models import AdminActor, AdminCatalogQuery, AdminCatalogRow
from registry.domain.enums import CatalogStatus
from registry.domain.labels import (
    COLOR_LABELS,
    COVERAGE_LABELS,
    KIND_LABELS,
    PUBLICATION_TYPE_LABELS,
    STATUS_LABELS,
)
from registry.domain.lifecycle import build_allowed_actions
from registry.domain.permissions import build_permissions
from registry.repositories.protocols import ActorStore, AdminReader
from registry.schemas.admin import (
    CatalogDetail,
    CatalogListItem,
    CatalogListResponse,
    CountryOption,
    CountryRef,
    FilterOptionsResponse,
    LanguageOption,
    LanguageRef,
    LinkItem,
    MeResponse,
    Option,
    PlaceResponse,
    StatsResponse,
    StatusCounts,
    SubdivisionRef,
    Submitter,
)
from registry.services.admin_identity import AdminIdentity
from registry.services.language_names import build_language_name

#: Who may see a submitter's name and email: the roles that review submissions by publishing them.
SUBMITTER_PERMISSION = "catalog.publish"

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
        email=actor.email,
        name=actor.name or actor.email.partition("@")[0],
        role=actor.role.value,
        permissions=sorted(build_permissions(actor.role)),
    )


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


async def resolve_admin_catalog_detail(
    reader: AdminReader, actor: AdminActor, catalog_id: str, *, base_url: str
) -> CatalogDetail:
    """One catalog in any status. An id that is not a UUID is simply not found: it comes from a
    page URL, where a hand-edited link should read as "no such catalog"."""
    try:
        normalised = str(uuid.UUID(catalog_id))
    except ValueError:
        raise NotFoundError(f"No catalog with id {catalog_id}.") from None
    detail = await reader.fetch_admin_catalog(normalised)
    if detail is None:
        raise NotFoundError(f"No catalog with id {normalised}.")
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
