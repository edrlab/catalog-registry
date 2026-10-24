"""What the admin repositories return and the admin service reads. Plain data, no database types."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from registry.domain.enums import ActorRole, CatalogStatus

#: `sort` values of the catalog list, and the page-size cap.
SORT_VALUES: Final = ("name", "-name", "kind", "status")
MAX_PER_PAGE: Final = 100
#: A page number that would overflow the database's 64-bit offset is a 422, not a 500.
MAX_PAGE: Final = 100_000
#: Each word is a scan of every catalog, so a long search text is cut here.
MAX_QUERY_WORDS: Final = 8


@dataclass(frozen=True, slots=True)
class AdminActor:
    id: str
    email: str
    name: str | None
    role: ActorRole
    is_active: bool


@dataclass(frozen=True, slots=True)
class AdminCatalogQuery:
    #: The search text split into words, each of which must appear somewhere (in any order).
    words: Sequence[str]
    statuses: Sequence[CatalogStatus]
    kinds: Sequence[str]
    coverages: Sequence[str]
    publication_types: Sequence[str]
    countries: Sequence[str]
    languages: Sequence[str]
    recommended: bool | None
    sort: str
    page: int
    per_page: int


@dataclass(frozen=True, slots=True)
class AdminCatalogRow:
    id: str
    name: str
    host: str | None
    color: str
    icon_url: str | None
    kinds: Sequence[str]
    coverage: str | None
    city: str | None
    subdivision: str | None
    country_code: str | None
    country_name: str | None
    languages: Sequence[str]
    status: CatalogStatus
    recommended: bool
    version: int


@dataclass(frozen=True, slots=True)
class AdminCatalogPage:
    #: Every match, not just this page's, and still right on a page past the end.
    total: int
    rows: Sequence[AdminCatalogRow]


@dataclass(frozen=True, slots=True)
class AdminStats:
    by_status: dict[str, int]
    recommended: int


@dataclass(frozen=True, slots=True)
class UsedCountry:
    code: str
    name: str
    catalog_count: int


@dataclass(frozen=True, slots=True)
class UsedLanguage:
    tag: str
    catalog_count: int


@dataclass(frozen=True, slots=True)
class UsedPlaces:
    countries: Sequence[UsedCountry]
    languages: Sequence[UsedLanguage]
