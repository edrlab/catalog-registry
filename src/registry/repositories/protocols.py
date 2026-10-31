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
    AdminStats,
    UsedPlaces,
)
from registry.domain.catalog_view import CatalogLike
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


class AdminReader(Protocol):
    async def fetch_admin_stats(self) -> AdminStats: ...

    async def fetch_used_places(self) -> UsedPlaces: ...

    async def fetch_admin_catalogs(self, query: AdminCatalogQuery) -> AdminCatalogPage: ...

    async def fetch_admin_catalog(self, catalog_id: str) -> AdminCatalogDetail | None: ...
