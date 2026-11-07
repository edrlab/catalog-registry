"""What the service layer needs from persistence.

Read and write are separate protocols (interface segregation): the public feed depends on
`CatalogReader` alone, so a read-only endpoint has no path to a delete. The back office in
v1.0 is what depends on both.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from registry.db.models.catalog import Catalog
from registry.domain.admin_models import (
    AdminActor,
    AdminCatalogDetail,
    AdminCatalogPage,
    AdminCatalogQuery,
    AdminCatalogState,
    AdminCountryDetail,
    AdminCountryPage,
    AdminStats,
    AdminUser,
    UsedPlaces,
)
from registry.domain.catalog_view import CatalogLike
from registry.domain.enums import ActorRole, CatalogStatus
from registry.domain.fetching import FetchResult
from registry.domain.restrictions import NO_RESTRICTIONS, Restrictions
from registry.domain.search_query import ParsedQuery


class CatalogReader(Protocol):
    async def fetch_recommended_catalogs(self) -> Sequence[CatalogLike]: ...


class CatalogLoader(Protocol):
    async def load_catalog_by_id(self, catalog_id: uuid.UUID) -> CatalogLike: ...


class CatalogWriter(Protocol):
    async def persist_catalog(self, document: dict[str, Any], *, recommended: bool) -> Catalog: ...


@dataclass(frozen=True, slots=True)
class SearchPage:
    #: Every match, not just this page's. Still reported on a page past the end.
    total: int
    catalogs: Sequence[CatalogLike]


class CatalogSearcher(Protocol):
    async def search_catalogs(
        self,
        query: ParsedQuery,
        *,
        limit: int,
        offset: int,
        restrictions: Restrictions = NO_RESTRICTIONS,
    ) -> SearchPage: ...


class ActorStore(Protocol):
    async def persist_actor_login(
        self, *, email: str, name: str | None, issuer: str, subject: str | None
    ) -> AdminActor: ...


class ProfileStore(Protocol):
    async def persist_actor_name(self, actor_id: str, name: str | None) -> None: ...


class UserStore(Protocol):
    """People, for admins. A change locks the active admins, then the person, then commits."""

    async def fetch_admin_users(self) -> list[AdminUser]: ...

    async def lock_active_admins(self) -> list[str]: ...

    async def lock_admin_user(self, user_id: str) -> AdminUser | None: ...

    async def persist_user_role(self, user_id: str, role: ActorRole) -> None: ...

    async def persist_user_active(self, user_id: str, active: bool) -> None: ...

    async def commit_user_change(self) -> None: ...


class AdminReader(Protocol):
    async def fetch_admin_stats(self) -> AdminStats: ...

    async def fetch_used_places(self) -> UsedPlaces: ...

    async def fetch_admin_catalogs(self, query: AdminCatalogQuery) -> AdminCatalogPage: ...

    async def fetch_admin_catalog(self, catalog_id: str) -> AdminCatalogDetail | None: ...

    async def fetch_admin_countries(
        self, q: str | None, page: int, per_page: int
    ) -> AdminCountryPage: ...

    async def fetch_admin_country(self, code: str) -> AdminCountryDetail | None: ...

    async def fetch_admin_catalog_by_href(
        self, href: str, derived_id: str
    ) -> tuple[str, str] | None:
        """(id, name) of the catalog using *href* as its identity, else of the catalog whose id
        is *derived_id* (the id a catalog created from *href* would get)."""
        ...


class FeedFetcher(Protocol):
    """One outbound fetch with the registry's protections (`network/safe_client.py`).
    Raises `FetchError`."""

    async def fetch(
        self,
        url: str,
        *,
        accept: str,
        max_bytes: int,
        deadline: float,
        method: str = "GET",
        truncate: bool = False,
    ) -> FetchResult: ...


class AdminWriter(Protocol):
    """One write: lock the row, then change it, then commit. Leaving without a commit rolls back."""

    async def lock_admin_catalog(self, catalog_id: str) -> AdminCatalogState | None: ...

    async def persist_catalog_status(self, catalog_id: str, status: CatalogStatus) -> None: ...

    async def persist_catalog_recommended(self, catalog_id: str, recommended: bool) -> None: ...

    async def fetch_unknown_places(
        self, country: str | None, subdivisions: Sequence[str]
    ) -> tuple[bool, list[str], list[str]]:
        """Whether *country* is known, the unknown subdivision codes, and the known ones that are
        not in *country* (every known one when *country* is None)."""
        ...

    async def persist_new_catalog(
        self, document: dict[str, Any], catalog_id: uuid.UUID, *, publish: bool
    ) -> None: ...

    async def fetch_catalog_document(self, catalog_id: str) -> dict[str, Any]: ...

    async def persist_catalog_document(self, catalog_id: str, document: dict[str, Any]) -> None: ...

    async def persist_catalog_deletion(self, catalog_id: str) -> None: ...

    async def lock_country_types(self, code: str) -> list[str] | None: ...

    async def persist_country_types(self, code: str, keys: Sequence[str]) -> None: ...

    async def commit_admin_write(self) -> None: ...
