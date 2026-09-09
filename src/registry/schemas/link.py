"""Wire contract for a link. Pydantic v2.

These models exist to describe the response in OpenAPI, not to enforce it, the JSON Schemas
under `schema/` are the contract, and the contract tests are the enforcement. What they add is
a machine-readable description for the clients this registry is built for.

Field names follow the wire, via aliases where the wire is camelCase. FastAPI serialises
response models with `by_alias=True`, so the alias is what reaches the client.
"""

from pydantic import BaseModel, ConfigDict, Field

from registry.domain.enums import LinkRel


class LinkResponse(BaseModel):
    """Mirrors the Readium link object, narrowed to what this registry emits."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    href: str
    #: `type` on the wire; renamed here because it shadows a builtin.
    media_type: str | None = Field(default=None, alias="type")
    rel: LinkRel
    templated: bool | None = None
    title: str | None = None
