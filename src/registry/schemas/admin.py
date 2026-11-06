"""The admin API's wire contract. Whitelist projection: a response lists its fields."""

import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
)
from pydantic_core import PydanticCustomError

from registry.domain.enums import (
    CatalogColor,
    CatalogKind,
    CatalogStatus,
    CoverageScope,
    PublicationType,
)


class AdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldProblem(AdminModel):
    field: str
    message: str


class ProblemDocument(BaseModel):
    """An RFC 9457 problem document: how every `/admin` error is sent, as problem+json."""

    type: str
    title: str
    status: int
    detail: str
    #: A URN naming this occurrence; it carries the request id, so a pasted body can be traced.
    instance: str | None = None
    #: Only on a 422: the invalid query fields.
    errors: list[FieldProblem] | None = None


class MeResponse(AdminModel):
    #: The caller's actor id: the users page marks "you" with it.
    id: str
    email: str
    #: The chosen name, or the email's local part when none is chosen (`name_is_set`).
    name: str
    name_is_set: bool
    role: str
    permissions: list[str]
    is_active: bool
    created_at: datetime.datetime | None
    #: The previous request's time, not a sign-in: null on the first visit.
    last_seen_at: datetime.datetime | None
    identity_issuer: str | None
    identity_subject: str | None


class StatusCounts(AdminModel):
    draft: int
    published: int
    unpublished: int
    archived: int


class StatsResponse(AdminModel):
    by_status: StatusCounts
    #: Published and recommended.
    recommended: int


class CountryRef(AdminModel):
    code: str
    name: str | None


class PlaceResponse(AdminModel):
    city: str | None
    subdivision: str | None
    country: CountryRef | None


class LanguageRef(AdminModel):
    tag: str
    name: str


class CatalogListItem(AdminModel):
    id: str
    name: str
    host: str | None
    color: str
    icon_url: str | None
    kinds: list[str]
    coverage: str | None
    place: PlaceResponse
    languages: list[LanguageRef]
    status: CatalogStatus
    recommended: bool
    version: int
    allowed_actions: list[str]
    public_url: str | None


class LinkItem(AdminModel):
    rel: str
    href: str
    type: str | None
    title: str | None
    templated: bool
    authentication: bool


class SubdivisionRef(AdminModel):
    code: str
    #: The English name, when the reference data has one.
    name: str | None


class Submitter(AdminModel):
    name: str | None
    email: str | None


class CatalogDetail(CatalogListItem):
    description: str | None
    publication_types: list[str]
    subdivisions: list[SubdivisionRef]
    links: list[LinkItem]
    created_at: datetime.datetime
    updated_at: datetime.datetime
    published_at: datetime.datetime | None
    #: Only for a role that reviews submissions; otherwise null. It is a third party's email.
    submitter: Submitter | None


class CatalogListResponse(AdminModel):
    items: list[CatalogListItem]
    total: int
    page: int
    per_page: int


class Option(AdminModel):
    value: str
    label: str


class CountryOption(AdminModel):
    code: str
    name: str
    catalog_count: int


class LanguageOption(AdminModel):
    tag: str
    name: str
    catalog_count: int


class FilterOptionsResponse(AdminModel):
    statuses: list[Option]
    kinds: list[Option]
    coverages: list[Option]
    publication_types: list[Option]
    colors: list[Option]
    countries: list[CountryOption]
    languages: list[LanguageOption]


class CountryListItem(AdminModel):
    code: str
    name: str
    #: The total `/admin/catalogs?country=CODE` shows by default.
    published_catalog_count: int


class CountryListResponse(AdminModel):
    items: list[CountryListItem]
    total: int
    page: int
    per_page: int


class SubdivisionTypeOption(AdminModel):
    #: ISO 3166-2's type name, lowercase.
    key: str
    label: str


class SubdivisionTypeCount(SubdivisionTypeOption):
    subdivision_count: int
    #: The subdivisions counted, by English name then code; as many as `subdivision_count`.
    subdivisions: list[SubdivisionRef]


class KindCounts(AdminModel):
    """One field per `CatalogKind`, so generated clients get typed keys. A unit test keeps the
    fields equal to the enum."""

    open: int
    public: int
    academic: int
    school: int
    specialized: int


class CountryKpis(AdminModel):
    by_status: StatusCounts
    #: Published and recommended.
    recommended: int
    #: Published catalogs; one with two kinds counts in both.
    by_kind: KindCounts


class CountryDetail(AdminModel):
    code: str
    name: str
    official_languages: list[LanguageRef]
    subdivision_types: list[SubdivisionTypeCount]
    available_subdivision_types: list[SubdivisionTypeOption]
    kpis: CountryKpis


#: The version a writer last saw. Strict: `"3"` or `true` is a client bug, not a version.
SeenVersion = Annotated[StrictInt, Field(ge=1)]


class TransitionRequest(AdminModel):
    action: Literal["publish", "unpublish", "archive", "unarchive"]
    version: SeenVersion


class TransitionResponse(AdminModel):
    catalog: CatalogDetail
    #: What reverses this change in one step, or null (the lifecycle contract's undo rule).
    undo_action: Literal["publish", "unpublish", "unarchive"] | None


class RecommendedRequest(AdminModel):
    recommended: StrictBool
    version: SeenVersion


#: No NUL: Postgres `text` cannot store it, and it would surface as a 500.
_NO_NUL = r"^[^\x00]*$"
#: A one-line value: no control characters at all.
_NO_CONTROL = r"^[^\x00-\x1f\x7f]*$"
CatalogName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=_NO_CONTROL)
]
#: Newlines and tabs are allowed in a description.
LongText = Annotated[str, StringConstraints(max_length=2000, pattern=_NO_NUL)]
ShortText = Annotated[str, StringConstraints(max_length=300, pattern=_NO_NUL)]
#: A media type; parameters are allowed (`application/opds+json;profile=…`).
MediaType = Annotated[str, StringConstraints(max_length=255, pattern=_NO_CONTROL)]
#: Letters only is checked by the shared document checks, which the seed applies too.
CityText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100, pattern=_NO_NUL)]
Href = Annotated[str, StringConstraints(min_length=1, max_length=2048, pattern=_NO_NUL)]
#: Shape and case are checked by the shared document checks, which say what the code should be.
PlaceCode = Annotated[str, StringConstraints(max_length=10, pattern=_NO_NUL)]
LanguageTag = Annotated[str, StringConstraints(max_length=35, pattern=_NO_NUL)]


class LinkInput(AdminModel):
    #: `self` is the registry's own URL, built when the catalog is served.
    rel: Literal["catalog", "shelf", "icon", "authenticate", "alternate", "profile", "search"]
    href: Href
    type: MediaType | None = None
    title: ShortText | None = None
    templated: StrictBool = False


Kinds = Annotated[list[CatalogKind], Field(min_length=1, max_length=10)]
Links = Annotated[list[LinkInput], Field(min_length=1, max_length=50)]
PlaceCodes = Annotated[list[PlaceCode], Field(max_length=50)]
LanguageTags = Annotated[list[LanguageTag], Field(max_length=50)]
PublicationTypes = Annotated[list[PublicationType], Field(max_length=10)]


class CatalogCreateRequest(AdminModel):
    """A new catalog. The same checks as the seed follow."""

    name: CatalogName
    description: LongText | None = None
    color: CatalogColor = CatalogColor.GRAY
    kinds: Kinds
    coverage: CoverageScope | None = None
    country: PlaceCode | None = None
    city: CityText | None = None
    subdivisions: PlaceCodes = []
    languages: LanguageTags = []
    publication_types: PublicationTypes = []
    links: Links
    publish: StrictBool = False


#: Fields a PATCH may not set to `null`: required ones, and lists, which are emptied with `[]`.
_NOT_NULLABLE = (
    "name",
    "color",
    "kinds",
    "subdivisions",
    "languages",
    "publication_types",
    "links",
)


class CatalogUpdateRequest(AdminModel):
    """Some fields of a catalog, and the version they were read at. An absent field is unchanged;
    `null` clears an optional one; a list replaces the whole list."""

    version: SeenVersion
    name: CatalogName | None = None
    description: LongText | None = None
    color: CatalogColor | None = None
    kinds: Kinds | None = None
    coverage: CoverageScope | None = None
    country: PlaceCode | None = None
    city: CityText | None = None
    subdivisions: PlaceCodes | None = None
    languages: LanguageTags | None = None
    publication_types: PublicationTypes | None = None
    links: Links | None = None

    @field_validator(*_NOT_NULLABLE)
    @classmethod
    def _refuse_null(cls, value: Any) -> Any:
        # Runs only on a value that was sent: an absent field keeps its default and is unchanged.
        if value is None:
            raise PydanticCustomError("not_nullable", "Cannot be null")
        return value


# --- feed checks ----------------------------------------------------------------------------------


class FeedCheckRequest(AdminModel):
    url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
    #: Re-check an existing catalog: the report then carries `changes`.
    catalog_id: str | None = None


class FeedCheckStage(AdminModel):
    key: Literal["address", "fetch", "feed", "links"]
    label: str
    state: Literal["passed", "warned", "failed", "skipped"]
    duration_ms: int


class FeedCheckFinding(AdminModel):
    stage: Literal["address", "fetch", "feed", "links"]
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    rel: str | None = None
    url: str | None = None
    pointer: str | None = None


class FeedCheckSummary(AdminModel):
    errors: int
    warnings: int
    info: int


class FeedCheckLink(AdminModel):
    rel: str
    href: str
    type: str | None = None
    title: str | None = None
    templated: bool = False
    #: True answered, False dead (unticked by default), None not checked (the cap).
    active: bool | None


class FeedCheckFields(AdminModel):
    name: str | None
    description: str | None
    languages: list[str]
    links: list[FeedCheckLink]


class FeedCheckDuplicate(AdminModel):
    id: str
    name: str


class FeedValueChange(AdminModel):
    field: Literal["name", "description", "languages"]
    catalog: str | list[str] | None
    feed: str | list[str] | None


class FeedLinksChange(AdminModel):
    field: Literal["links"]
    added: list[FeedCheckLink]
    removed: list[FeedCheckLink]


class FeedCheckResponse(AdminModel):
    url: str
    final_url: str | None
    checked_at: datetime.datetime
    duration_ms: int
    verdict: Literal["ok", "warnings", "blocked"]
    format: Literal["opds2", "html"] | None
    stages: list[FeedCheckStage]
    findings: list[FeedCheckFinding]
    summary: FeedCheckSummary
    feed: FeedCheckFields | None
    duplicate: FeedCheckDuplicate | None
    changes: list[FeedValueChange | FeedLinksChange] | None = None


# --- users ----------------------------------------------------------------------------------------


class UserItem(AdminModel):
    id: str
    email: str
    name: str | None
    role: Literal["viewer", "editor", "admin"]
    is_active: bool
    last_seen_at: datetime.datetime | None
    created_at: datetime.datetime
    allowed_actions: list[Literal["change_role", "deactivate", "reactivate"]]


class UserListResponse(AdminModel):
    items: list[UserItem]


class UserRoleRequest(AdminModel):
    role: Literal["viewer", "editor", "admin"]


class SubdivisionTypesRequest(AdminModel):
    """The types a country offers, from its `available_subdivision_types`."""

    keys: Annotated[
        list[Annotated[str, StringConstraints(max_length=100, pattern=_NO_NUL)]],
        Field(max_length=100),
    ]


class MeUpdateRequest(AdminModel):
    """Your own display name. `null` or blank clears it."""

    name: (
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True, max_length=100, pattern=r"^[^\x00-\x1f\x7f]*$"
            ),
        ]
        | None
    )
