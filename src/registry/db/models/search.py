"""The derived search table (ADR-051, ADR-052, ADR-056).

**Read-only from the application.** Database triggers own every row: `refresh_catalog_search()`
rebuilds it in the same transaction as any change to a catalog's title, city, country, status or
subdivisions. Nothing here inserts, updates or deletes, and a test that does is testing a thing
the application must never do. The model exists so `alembic check` and autogenerate know the
table and its indexes, and do not propose dropping them.
"""

import uuid

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from registry.db.base import Base


class CatalogSearch(Base):
    __tablename__ = "catalog_search"
    __table_args__ = (
        Index("ix_catalog_search_document", "document", postgresql_using="gin"),
        Index(
            "ix_catalog_search_names_trgm",
            "names",
            postgresql_using="gin",
            postgresql_ops={"names": "public.gin_trgm_ops"},
        ),
    )

    catalog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalogs.id", ondelete="CASCADE"), primary_key=True
    )
    #: Word half. Labels: A title, B country names, C subdivision names and city, D unused.
    document: Mapped[str] = mapped_column(TSVECTOR, nullable=False)
    #: Trigram half: `fold()` of title, city, subdivision names and country names.
    names: Mapped[str] = mapped_column(Text, nullable=False)
