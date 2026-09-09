"""Wire contract for the top-level feed."""

from pydantic import BaseModel, ConfigDict, Field

from registry.schemas.catalog import CatalogResponse
from registry.schemas.link import LinkResponse


class FeedMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: str
    #: `numberOfItems` on the wire; aliased so the Python side stays PEP 8.
    number_of_items: int = Field(default=0, alias="numberOfItems")


class FeedResponse(BaseModel):
    """the top-level feed does not paginate, so there is no `itemsPerPage`."""

    model_config = ConfigDict(extra="forbid")

    metadata: FeedMetadata
    links: list[LinkResponse]
    catalogs: list[CatalogResponse]
