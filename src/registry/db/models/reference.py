"""ISO 3166 reference tables. Populated by a data migration, not a seed script, so every
environment including CI has them without an extra step."""

from sqlalchemy import CHAR, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from registry.db.base import Base


class Country(Base):
    __tablename__ = "countries"

    alpha2: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    alpha3: Mapped[str] = mapped_column(CHAR(3), unique=True)
    numeric3: Mapped[str] = mapped_column(CHAR(3))
    #: false for dissolved states — keeps historical foreign keys valid rather than orphaned.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class Subdivision(Base):
    __tablename__ = "subdivisions"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    country_alpha2: Mapped[str] = mapped_column(
        CHAR(2), ForeignKey("countries.alpha2"), nullable=False
    )
    #: Subdivisions nest — France has regions and departments. Load-bearing for v1.2 ranking.
    parent_code: Mapped[str | None] = mapped_column(
        String(6), ForeignKey("subdivisions.code"), nullable=True
    )
    #: Nullable: which ISO 3166-2 class to standardise on is open (Q14). It only matters
    #: once subdivisions drive filtering, which is v1.2.
    subdivision_type: Mapped[str | None] = mapped_column(nullable=True)
