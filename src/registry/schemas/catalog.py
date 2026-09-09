"""Wire contract for a catalog document."""

from pydantic import BaseModel, ConfigDict, Field

from registry.domain.enums import CatalogColor, CatalogKind, CoverageScope, PublicationType
from registry.schemas.link import LinkResponse


class CatalogMetadata(BaseModel):
    """`extra="forbid"` mirrors `additionalProperties: false` in `catalog.schema.json`.

    R3 is enforced upstream by whitelist projection in the renderer; this is a second line,
    and it is the one that shows up in OpenAPI.

    Declaration order is emission order.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: str
    kind: list[CatalogKind]
    description: str | None = None
    color: CatalogColor
    supported_languages: list[str] | None = Field(default=None, alias="supportedLanguages")
    publication_types: list[PublicationType] | None = Field(default=None, alias="publicationTypes")
    country: str | None = None
    subdivisions: list[str] | None = None
    city: str | None = None
    #: ADR-032 — absent means not declared, which is not the same as `global`.
    coverage: CoverageScope | None = None


class CatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: CatalogMetadata
    links: list[LinkResponse]
