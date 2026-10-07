"""Importing this package registers every model on `Base.metadata`.

Alembic's `env.py` and the test harness both need the full metadata, and a model that is
never imported is a table autogenerate silently proposes dropping.
"""

from registry.db.models.catalog import (
    Catalog,
    CatalogKindRow,
    CatalogLanguageRow,
    CatalogPublicationTypeRow,
    CatalogSubdivisionRow,
)
from registry.db.models.link import Link
from registry.db.models.reference import (
    Country,
    CountryLanguage,
    CountryName,
    CountrySubdivisionType,
    Subdivision,
    SubdivisionName,
)
from registry.db.models.search import CatalogSearch

__all__ = [
    "Catalog",
    "CatalogKindRow",
    "CatalogLanguageRow",
    "CatalogPublicationTypeRow",
    "CatalogSearch",
    "CatalogSubdivisionRow",
    "Country",
    "CountryLanguage",
    "CountryName",
    "CountrySubdivisionType",
    "Link",
    "Subdivision",
    "SubdivisionName",
]
