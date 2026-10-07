"""A catalog as the public reads hand it to the renderer: plain values, no database in sight.

The feed, one catalog and search each fetch a page of catalogs in one statement (ADR-060,
ADR-062). Turning each row into an instrumented SQLAlchemy object cost about nine tenths of the
Python time of a 1,000 catalog feed (measured with a profiler): a catalog and a row object per kind,
language, subdivision and link, each carrying change tracking nobody uses on a read. These are
slotted tuples with the same attribute names, so the renderer and the feed's language sort read
them exactly as they read the ORM objects the importers load.

`CatalogLike` and the `...Like` protocols are what the renderer needs, so both kinds fit.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, Protocol

from registry.domain.enums import (
    CatalogColor,
    CatalogKind,
    CoverageScope,
    LinkRel,
    PublicationType,
)


class KindLike(Protocol):
    @property
    def kind(self) -> CatalogKind: ...


class PublicationTypeLike(Protocol):
    @property
    def publication_type(self) -> PublicationType: ...


class LanguageLike(Protocol):
    @property
    def language_tag(self) -> str: ...


class SubdivisionLike(Protocol):
    @property
    def subdivision_code(self) -> str: ...


class LinkLike(Protocol):
    @property
    def href(self) -> str: ...
    @property
    def media_type(self) -> str | None: ...
    @property
    def rel(self) -> LinkRel: ...
    @property
    def templated(self) -> bool: ...
    @property
    def title(self) -> str | None: ...


class CatalogLike(Protocol):
    """What `render_catalog` and the feed's language sort read. `created_at` is internal: it is
    never rendered (R3), only used to order catalogs that rank equally."""

    @property
    def id(self) -> uuid.UUID: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def title(self) -> str: ...
    @property
    def description(self) -> str | None: ...
    @property
    def color(self) -> CatalogColor: ...
    @property
    def country_code(self) -> str | None: ...
    @property
    def city(self) -> str | None: ...
    @property
    def coverage(self) -> CoverageScope | None: ...
    @property
    def kinds(self) -> Sequence[KindLike]: ...
    @property
    def publication_types(self) -> Sequence[PublicationTypeLike]: ...
    @property
    def languages(self) -> Sequence[LanguageLike]: ...
    @property
    def subdivisions(self) -> Sequence[SubdivisionLike]: ...
    @property
    def links(self) -> Sequence[LinkLike]: ...


class KindRow(NamedTuple):
    kind: CatalogKind


class PublicationTypeRow(NamedTuple):
    publication_type: PublicationType


class LanguageRow(NamedTuple):
    language_tag: str


class SubdivisionRow(NamedTuple):
    subdivision_code: str


class LinkRow(NamedTuple):
    href: str
    media_type: str | None
    rel: LinkRel
    templated: bool
    title: str | None


@dataclass(frozen=True, slots=True)
class CatalogView:
    id: uuid.UUID
    created_at: datetime
    title: str
    description: str | None
    color: CatalogColor
    country_code: str | None
    city: str | None
    coverage: CoverageScope | None
    kinds: Sequence[KindLike]
    publication_types: Sequence[PublicationTypeLike]
    languages: Sequence[LanguageLike]
    subdivisions: Sequence[SubdivisionLike]
    links: Sequence[LinkLike]
