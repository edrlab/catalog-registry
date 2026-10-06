"""Catalog persistence. The only module that writes SQL for catalogs.

Two kinds of read, deliberately different:

* **The public reads** (the top-level feed, one catalog) are one statement each (ADR-062): the
  catalogs and every child in a single round trip, mapped to the `Catalog` the renderer reads. They
  run on the read pool, which carries a timeout and read-only mode on the connection.
* **The importers' lookups** (`fetch_catalog_by_identity_*`) load attached ORM objects with
  `selectinload`, because the import then changes them. `lazy="raise_on_sql"` on the models turns a
  forgotten eager load into a loud failure rather than an N+1 nobody notices.
"""

import uuid
from collections.abc import Sequence
from typing import Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from registry.core.errors import NotFoundError
from registry.db.models.catalog import Catalog
from registry.domain.links import IDENTITY_RELS
from registry.repositories.catalog_rows import (
    CATALOG_COLUMNS,
    build_catalog_from_row,
    fetch_rows,
)

#: The feed and a single catalog may take a moment on a big registry, but not forever: a read that
#: runs past this is cancelled and answered with a 503 (ADR-062). Search has its own, much shorter.
READ_STATEMENT_TIMEOUT_MS: Final = 10_000

#: Sent once per connection to the read pool (`build_read_engine`). Read-only because these
#: endpoints must never write.
READ_CONNECTION_SETTINGS: Final = {
    "statement_timeout": str(READ_STATEMENT_TIMEOUT_MS),
    "default_transaction_read_only": "on",
}

#: The top-level feed's only query. Hits `ix_catalogs_recommended`. Newest first, then title: the
#: seed writes `created_at` staggered by position in the file (the feed's order), and title is a
#: tie-break for determinism, since rows inserted in one transaction can share a `created_at`.
_RECOMMENDED = text(
    f"""
SELECT {CATALOG_COLUMNS}
  FROM catalogs c
 WHERE c.recommended AND c.status = 'active'
 ORDER BY c.created_at DESC, c.title
"""
)

#: `status` is filtered because `GET /catalogs/{id}` is unauthenticated: a suggested catalog is
#: somebody's unreviewed submission, and an id is not an access control. `recommended` is not: an
#: active catalog that is simply not recommended is a real, published catalog.
_ACTIVE_BY_ID = text(
    f"""
SELECT {CATALOG_COLUMNS}
  FROM catalogs c
 WHERE c.id = :catalog_id AND c.status = 'active'
"""
)

EAGER_COLLECTIONS = (
    selectinload(Catalog.kinds),
    selectinload(Catalog.publication_types),
    selectinload(Catalog.languages),
    selectinload(Catalog.subdivisions),
    selectinload(Catalog.links),
)


class CatalogRepository:
    """Satisfies `CatalogReader`. The service owns the transaction, not this class, a
    repository that commits cannot be composed into a larger unit of work."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_recommended_catalogs(self) -> Sequence[Catalog]:
        """The top-level feed's only query, in one statement. The service applies the primary,
        language-based sort on top of this order."""
        rows = await fetch_rows(self._session, _RECOMMENDED, {})
        return [build_catalog_from_row(row) for row in rows]

    async def fetch_catalog_by_id(self, catalog_id: uuid.UUID) -> Catalog | None:
        """Read one published catalog in one statement. None when it does not exist, or is not
        public."""
        rows = await fetch_rows(self._session, _ACTIVE_BY_ID, {"catalog_id": catalog_id})
        return build_catalog_from_row(rows[0]) if rows else None

    async def load_catalog_by_id(self, catalog_id: uuid.UUID) -> Catalog:
        """Read one catalog, raising if it is absent. `load_` raises where `fetch_` returns
        None."""
        catalog = await self.fetch_catalog_by_id(catalog_id)
        if catalog is None:
            raise NotFoundError(f"no catalog with id {catalog_id}")
        return catalog

    async def fetch_catalog_by_identity_id(self, catalog_id: uuid.UUID) -> Catalog | None:
        """The import's lookup for a document that names its own id. No `status` filter.

        `fetch_catalog_by_id` is the *public* read and hides anything not `active`. Using it
        here would make the seed unable to see a `suggested` row holding this id, so the import
        would try to insert a duplicate primary key rather than update it. The seed runs as the
        operator, not as a reader; `status` is its business.
        """
        statement = select(Catalog).where(Catalog.id == catalog_id).options(*EAGER_COLLECTIONS)
        return (await self._session.scalars(statement)).unique().first()

    async def fetch_catalog_by_identity_href(self, href: str) -> Catalog | None:
        """Look a catalog up by the identity the seed and `add` both upsert on.

        Constrained to `IDENTITY_RELS`. Matching *any* link with this href would let a new
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
