"""What the admin repositories return and the admin service reads. Plain data, no database types."""

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from registry.domain.enums import ActorRole, CatalogStatus

#: The page-size cap of the catalog list.
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
    #: For the person's own profile. Absent where an actor is built without a login.
    created_at: datetime.datetime | None = None
    #: The request before this one: the registry sees requests, not sign-ins.
    previous_seen_at: datetime.datetime | None = None
    identity_issuer: str | None = None
    identity_subject: str | None = None


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
class AdminCatalogLink:
    rel: str
    href: str
    media_type: str | None
    title: str | None
    templated: bool
    authentication: bool


@dataclass(frozen=True, slots=True)
class AdminSubdivision:
    code: str
    name: str | None


@dataclass(frozen=True, slots=True)
class AdminCatalogDetail:
    """One catalog for the detail page: the list row, and what only the detail shows."""

    row: AdminCatalogRow
    description: str | None
    publication_types: Sequence[str]
    subdivisions: Sequence[AdminSubdivision]
    links: Sequence[AdminCatalogLink]
    created_at: datetime.datetime
    updated_at: datetime.datetime
    published_at: datetime.datetime | None
    submitter_name: str | None
    submitter_email: str | None


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


@dataclass(frozen=True, slots=True)
class AdminCatalogState:
    """What a write checks before it applies: the status the lifecycle reads, the version the
    caller must have seen."""

    status: CatalogStatus
    version: int


@dataclass(frozen=True, slots=True)
class AdminCountryRow:
    code: str
    name: str
    published_catalog_count: int


@dataclass(frozen=True, slots=True)
class AdminCountryPage:
    total: int
    rows: Sequence[AdminCountryRow]


@dataclass(frozen=True, slots=True)
class AdminCountryDetail:
    code: str
    name: str
    language_tags: Sequence[str]
    #: `(type, its subdivisions in the country)`, for the types the back office offers. English
    #: names, the rule the catalog detail uses.
    subdivision_types: Sequence[tuple[str, Sequence[AdminSubdivision]]]
    available_subdivision_types: Sequence[str]
    #: Every status, 0 included, like `AdminStats.by_status`.
    by_status: dict[str, int]
    recommended: int
    #: Published catalogs per kind; only kinds in use, the service fills the rest with 0.
    by_kind: dict[str, int]


@dataclass(frozen=True, slots=True)
class AdminUser:
    """One person in `actors`, as the users page shows them."""

    id: str
    email: str
    name: str | None
    role: ActorRole
    is_active: bool
    last_seen_at: datetime.datetime | None
    created_at: datetime.datetime
