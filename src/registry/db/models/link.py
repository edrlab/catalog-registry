"""Catalog links. ADR-013 — a link carries a single rel, not an array."""

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from registry.db.base import Base
from registry.domain.enums import LinkRel


class Link(Base):
    __tablename__ = "links"
    __table_args__ = (
        # Satisfies the schema's `uniqueItems` on the links array.
        UniqueConstraint("catalog_id", "rel", "href", name="uq_links_catalog_rel_href"),
        CheckConstraint("NOT templated OR rel = 'search'", name="templated_only_search"),
        Index("ix_links_catalog_id", "catalog_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default="gen_random_uuid()"
    )
    catalog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalogs.id", ondelete="CASCADE"), nullable=False
    )
    href: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    rel: Mapped[LinkRel] = mapped_column(
        Enum(LinkRel, name="link_rel", values_callable=lambda enum: [m.value for m in enum]),
        nullable=False,
    )
    templated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    #: ADR-014 — a flag, not stored credentials.
    authentication: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: No `position` column. Ordering is computed at render time from `rel` — a stored order
    #: drifts from spec intent the moment the spec changes.
