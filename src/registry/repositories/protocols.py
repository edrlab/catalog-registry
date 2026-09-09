"""What the service layer needs from persistence.

Read and write are separate protocols (interface segregation): the public feed depends on
`CatalogReader` alone, so a read-only endpoint has no path to a delete. The back office in
v1.0 is what depends on both.
"""

import uuid
from collections.abc import Sequence
from typing import Any, Protocol

from registry.db.models.catalog import Catalog


class CatalogReader(Protocol):
    async def fetch_recommended_catalogs(self) -> Sequence[Catalog]: ...


class CatalogLoader(Protocol):
    async def load_catalog_by_id(self, catalog_id: uuid.UUID) -> Catalog: ...


class CatalogWriter(Protocol):
    async def persist_catalog(self, document: dict[str, Any], *, recommended: bool) -> Catalog: ...
