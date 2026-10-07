"""ISO 3166 reference tables. Populated by a data migration, not a seed script, so every
environment including CI has them without an extra step."""

from sqlalchemy import CHAR, Boolean, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from registry.db.base import Base


class Country(Base):
    __tablename__ = "countries"

    alpha2: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    alpha3: Mapped[str] = mapped_column(CHAR(3), unique=True)
    numeric3: Mapped[str] = mapped_column(CHAR(3))
    #: false for dissolved states. Keeps historical foreign keys valid rather than orphaned.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class Subdivision(Base):
    __tablename__ = "subdivisions"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    country_alpha2: Mapped[str] = mapped_column(
        CHAR(2), ForeignKey("countries.alpha2"), nullable=False
    )
    #: Subdivisions nest. France has regions and departments. Load-bearing for v1.2 ranking.
    parent_code: Mapped[str | None] = mapped_column(
        String(6), ForeignKey("subdivisions.code"), nullable=True
    )
    #: Nullable: which ISO 3166-2 class to standardise on is still open. It only matters
    #: once subdivisions drive filtering, which is v1.2.
    subdivision_type: Mapped[str | None] = mapped_column(nullable=True)


# v0.2 search (ADR-052, ADR-053, ADR-055, ADR-057). Loaded by a data migration from pinned CLDR
# 48.2 and ISO 3166-2 snapshots; there is no write path from the application.


class CountryLanguage(Base):
    """Official languages of a country. Decides which place names a search document holds."""

    __tablename__ = "country_languages"
    __table_args__ = (
        CheckConstraint("language_tag = lower(language_tag)", name="language_tag_lowercase"),
    )

    country_code: Mapped[str] = mapped_column(
        CHAR(2), ForeignKey("countries.alpha2"), primary_key=True
    )
    language_tag: Mapped[str] = mapped_column(String(35), primary_key=True)


class CountryName(Base):
    """Several names per language are allowed (UK, United Kingdom), hence the three-column key."""

    __tablename__ = "country_names"
    __table_args__ = (
        CheckConstraint("language_tag = lower(language_tag)", name="language_tag_lowercase"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )

    country_code: Mapped[str] = mapped_column(
        CHAR(2), ForeignKey("countries.alpha2"), primary_key=True
    )
    language_tag: Mapped[str] = mapped_column(String(35), primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)


class SubdivisionName(Base):
    __tablename__ = "subdivision_names"
    __table_args__ = (
        CheckConstraint("language_tag = lower(language_tag)", name="language_tag_lowercase"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )

    subdivision_code: Mapped[str] = mapped_column(
        String(6), ForeignKey("subdivisions.code"), primary_key=True
    )
    language_tag: Mapped[str] = mapped_column(String(35), primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)


class CountrySubdivisionType(Base):
    """What the back office offers per country. Search does not read it."""

    __tablename__ = "country_subdivision_types"
    __table_args__ = (
        CheckConstraint(
            "subdivision_type = lower(subdivision_type)", name="subdivision_type_lowercase"
        ),
    )

    country_code: Mapped[str] = mapped_column(
        CHAR(2), ForeignKey("countries.alpha2"), primary_key=True
    )
    subdivision_type: Mapped[str] = mapped_column(String, primary_key=True)
