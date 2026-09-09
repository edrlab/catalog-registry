"""Catalog persistence. The only module that writes SQL for catalogs.

Every query returning catalogs eager-loads all five collections in the same round trip.
`selectinload` costs one line per relationship; retrofitting it means revisiting every query,
and `lazy="raise_on_sql"` on the models turns a forgotten one into a loud failure rather than
an N+1 nobody notices until the dataset grows.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from registry.core.errors import NotFoundError
from registry.db.models.catalog import Catalog
from registry.domain.enums import CatalogStatus, LinkRel

EAGER_COLLECTIONS = (
    selectinload(Catalog.kinds),
    selectinload(Catalog.publication_types),
    selectinload(Catalog.languages),
    selectinload(Catalog.subdivisions),
    selectinload(Catalog.links),
)


#: The rels a catalog is identified by across seed runs, preferred first. Mirrors
#: `cli/seed.py`, which decides *which* of them a document uses.
IDENTITY_RELS = (LinkRel.CATALOG, LinkRel.SHELF)


class CatalogRepository:
    """Satisfies `CatalogReader`. The service owns the transaction, not this class, a
    repository that commits cannot be composed into a larger unit of work."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_recommended_catalogs(self) -> Sequence[Catalog]:
        """The top-level feed's only query. Hits `ix_catalogs_recommended`.

        Ordered by case-folded title in the database so two identical requests produce
        byte-identical bodies without the renderer having to re-sort.
        """
        statement = (
            select(Catalog)
            .where(Catalog.recommended.is_(True), Catalog.status == CatalogStatus.ACTIVE)
            .options(*EAGER_COLLECTIONS)
            .order_by(Catalog.title)
        )
        return (await self._session.scalars(statement)).all()

    async def fetch_catalog_by_id(self, catalog_id: uuid.UUID) -> Catalog | None:
        """Read one published catalog. Returns None when it does not exist, or is not public.

        `GET /catalogs/{id}` is unauthenticated, so this filters on `status` for the same
        reason the feed does. A suggested catalog is somebody's unreviewed submission, and an
        id is not an access control. `recommended` is deliberately *not* filtered: an active
        catalog that is simply not recommended is still a real, published catalog, and the
        back office will need to link to one.
        """
        statement = (
            select(Catalog)
            .where(Catalog.id == catalog_id, Catalog.status == CatalogStatus.ACTIVE)
            .options(*EAGER_COLLECTIONS)
        )
        return (await self._session.scalars(statement)).unique().first()

    async def load_catalog_by_id(self, catalog_id: uuid.UUID) -> Catalog:
        """Read one catalog, raising if it is absent. `load_` raises where `fetch_` returns
        None."""
        catalog = await self.fetch_catalog_by_id(catalog_id)
        if catalog is None:
            raise NotFoundError(f"no catalog with id {catalog_id}")
        return catalog

    async def fetch_catalog_by_identity_href(self, href: str) -> Catalog | None:
        """Look a catalog up by the identity the seed upserts on. See `cli/seed.py`.

        Constrained to the identity rels. Matching *any* link with this href would let a new
        catalog whose feed URL happens to equal another catalog's `search` or `icon` target
        overwrite that unrelated row.
        """
        # Imported here, not at module level: it would be a circular import.
        from registry.db.models.link import Link  # noqa: PLC0415

        statement = (
            select(Catalog)
            .join(Link, Link.catalog_id == Catalog.id)
            .where(Link.href == href, Link.rel.in_(IDENTITY_RELS))
            .options(*EAGER_COLLECTIONS)
        )
        return (await self._session.scalars(statement)).unique().first()
