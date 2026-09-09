"""The catalog aggregate: the row, its four value collections, and its links.

Every relationship sets ``lazy="raise_on_sql"``. Under async a lazy load raises
``MissingGreenlet``, whose message points nowhere useful; ``raise_on_sql`` fails at the
access site with a clear error, during development, which is where you want it. It also
means an N+1 cannot happen silently, a query that forgot ``selectinload`` raises rather
than quietly issuing N+1.
"""

import datetime
import uuid

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from registry.db.base import Base
from registry.db.models.link import Link
from registry.domain.enums import (
    CatalogColor,
    CatalogKind,
    CatalogStatus,
    CoverageScope,
    PublicationType,
)


def _pg_enum(enum_type: type, name: str) -> Enum:
    """Native Postgres enum, keyed on the member *values* rather than their Python names."""
    return Enum(enum_type, name=name, values_callable=lambda enum: [m.value for m in enum])


class Catalog(Base):
    __tablename__ = "catalogs"
    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        CheckConstraint("country_code = upper(country_code)", name="country_uppercase"),
        CheckConstraint(
            "status <> 'active' OR published_at IS NOT NULL", name="published_when_active"
        ),
        # The top-level feed's only query. A partial index is the right shape over ten rows
        # and stays right as the table grows.
        Index(
            "ix_catalogs_recommended",
            "recommended",
            postgresql_where=text("recommended AND status = 'active'"),
        ),
        Index("ix_catalogs_country_code", "country_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    status: Mapped[CatalogStatus] = mapped_column(
        _pg_enum(CatalogStatus, "catalog_status"), nullable=False, server_default="suggested"
    )
    recommended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[CatalogColor] = mapped_column(
        _pg_enum(CatalogColor, "catalog_color"), nullable=False, server_default="gray"
    )
    #: Nullable, a global catalog belongs to no country.
    country_code: Mapped[str | None] = mapped_column(
        CHAR(2), ForeignKey("countries.alpha2"), nullable=True
    )
    #: Free text, not a relation. Cities become a table in v1.2.
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Nullable, no default. NULL means *not declared*; `global` means *worldwide*.
    #: Defaulting to `global` would be the registry asserting worldwide reach on a catalog's
    #: behalf, and it collapses a distinction geographic ranking will depend on.
    coverage: Mapped[CoverageScope | None] = mapped_column(
        _pg_enum(CoverageScope, "coverage_scope"), nullable=True
    )
    submitter_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitter_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Maintained by a trigger, not an ORM `onupdate`: the trigger also fires for the direct
    #: SQL the seed script and any future data migration perform.
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    kinds: Mapped[list["CatalogKindRow"]] = relationship(
        cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    publication_types: Mapped[list["CatalogPublicationTypeRow"]] = relationship(
        cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    languages: Mapped[list["CatalogLanguageRow"]] = relationship(
        cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    subdivisions: Mapped[list["CatalogSubdivisionRow"]] = relationship(
        cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    links: Mapped[list[Link]] = relationship(cascade="all, delete-orphan", lazy="raise_on_sql")


class CatalogKindRow(Base):
    """At least one kind per catalog. Enforced in the service, a table-level constraint
    cannot express "the collection is non-empty" without a deferred trigger."""

    __tablename__ = "catalog_kinds"
    __table_args__ = (Index("ix_catalog_kinds_catalog_id", "catalog_id"),)

    catalog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalogs.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[CatalogKind] = mapped_column(
        _pg_enum(CatalogKind, "catalog_kind"), primary_key=True
    )


class CatalogPublicationTypeRow(Base):
    __tablename__ = "catalog_publication_types"
    __table_args__ = (Index("ix_catalog_publication_types_catalog_id", "catalog_id"),)

    catalog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalogs.id", ondelete="CASCADE"), primary_key=True
    )
    publication_type: Mapped[PublicationType] = mapped_column(
        _pg_enum(PublicationType, "publication_type"), primary_key=True
    )


class CatalogLanguageRow(Base):
    __tablename__ = "catalog_languages"
    __table_args__ = (
        # Lowercasing made structural: an uppercase tag cannot be stored, so a comparison
        # against a lowercase Accept-Language cannot silently return an empty feed.
        CheckConstraint("language_tag = lower(language_tag)", name="language_tag_lowercase"),
        Index("ix_catalog_languages_catalog_id", "catalog_id"),
    )

    catalog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalogs.id", ondelete="CASCADE"), primary_key=True
    )
    #: RFC 5646's practical maximum for a well-formed tag. A sanity bound, not a business rule.
    language_tag: Mapped[str] = mapped_column(String(35), primary_key=True)


class CatalogSubdivisionRow(Base):
    __tablename__ = "catalog_subdivisions"
    __table_args__ = (
        CheckConstraint(
            "subdivision_code = upper(subdivision_code)", name="subdivision_code_uppercase"
        ),
        Index("ix_catalog_subdivisions_catalog_id", "catalog_id"),
    )

    catalog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalogs.id", ondelete="CASCADE"), primary_key=True
    )
    subdivision_code: Mapped[str] = mapped_column(
        String(6), ForeignKey("subdivisions.code"), primary_key=True
    )
