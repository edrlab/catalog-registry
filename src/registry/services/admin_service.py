"""What the admin API says, assembled from what the repositories read."""

from collections.abc import Mapping

from registry.core.errors import PermissionDeniedError
from registry.domain.admin_models import AdminActor, AdminCatalogQuery
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
    CatalogListItem,
    CatalogListResponse,
    CountryOption,
    CountryRef,
    FilterOptionsResponse,
    LanguageOption,
    LanguageRef,
    MeResponse,
    Option,
    PlaceResponse,
    StatsResponse,
    StatusCounts,
)
from registry.services.admin_identity import AdminIdentity
from registry.services.language_names import build_language_name

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
        items=[
            CatalogListItem(
                id=row.id,
                name=row.name,
                host=row.host,
                color=row.color,
                icon_url=row.icon_url,
                kinds=list(row.kinds),
                coverage=row.coverage,
                place=PlaceResponse(
                    city=row.city,
                    subdivision=row.subdivision,
                    country=(
                        CountryRef(code=row.country_code, name=row.country_name)
                        if row.country_code
                        else None
                    ),
                ),
                languages=[
                    LanguageRef(tag=tag, name=build_language_name(tag)) for tag in row.languages
                ],
                status=row.status,
                recommended=row.recommended,
                version=row.version,
                allowed_actions=build_allowed_actions(row.status, permissions),
                public_url=(
                    f"{base_url.rstrip('/')}/catalogs/{row.id}"
                    if row.status in _DIRECT_LINK_STATUSES
                    else None
                ),
            )
            for row in page.rows
        ],
        total=page.total,
        page=query.page,
        per_page=query.per_page,
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
