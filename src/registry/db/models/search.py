"""The derived search table.

**Read-only from the application.** Database triggers own every row: `refresh_catalog_search()`
rebuilds it in the same transaction as any change to a catalog's title, city, country, status,
subdivisions, links, kinds or publication types. Nothing here inserts, updates or deletes, and a
test that does is testing a thing the application must never do. The model exists so
`alembic check` and autogenerate know the table and its indexes, and do not propose dropping them.
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from registry.db.base import Base


class CatalogSearch(Base):
    __tablename__ = "catalog_search"
    __table_args__ = (
        Index("ix_catalog_search_title_tin", "title", postgresql_using="tin"),
        Index("ix_catalog_search_city_tin", "city", postgresql_using="tin"),
        # No length normalisation: a long `places` means a country with many languages.
        Index(
            "ix_catalog_search_places_tin",
            "places",
            postgresql_using="tin",
            postgresql_with={"b": 0},
        ),
    )

    catalog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalogs.id", ondelete="CASCADE"), primary_key=True
    )
    #: All three through `public.fold()`, one per ranking weight.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    #: Country and subdivision names, in English and the country's official languages.
    places: Mapped[str] = mapped_column(Text, nullable=False)
    #: The `restrict` rules (`domain/restrictions.py`), on the row so filtering is a column test.
    has_opds_catalog: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    audiobook_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
