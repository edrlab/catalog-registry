"""The admin API's wire contract. Whitelist projection (R3): a response lists its fields."""

import datetime

from pydantic import BaseModel, ConfigDict

from registry.domain.enums import CatalogStatus


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
    email: str
    name: str
    role: str
    permissions: list[str]


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
