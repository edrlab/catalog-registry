"""Wire contract for the top-level feed."""

from pydantic import BaseModel, ConfigDict, Field

from registry.schemas.catalog import CatalogResponse
from registry.schemas.link import LinkResponse


class FeedLinkResponse(LinkResponse):
    """A feed-level link. `rel` is free text because paging adds `first`, `previous` and `next`,
    which are not catalog link rels (`LinkRel` mirrors the `link_rel` database enum, and a
    catalog never stores a paging link)."""

    rel: str  # type: ignore[assignment]


class FeedMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: str
    #: `numberOfItems` on the wire; aliased so the Python side stays PEP 8.
    number_of_items: int = Field(default=0, alias="numberOfItems")
    #: Search results only; the top-level feed does not paginate.
    items_per_page: int | None = Field(default=None, alias="itemsPerPage")
    current_page: int | None = Field(default=None, alias="currentPage")


class FeedResponse(BaseModel):
    """The top-level feed and search results. Only search results carry the paging fields."""

    model_config = ConfigDict(extra="forbid")

    metadata: FeedMetadata
    links: list[FeedLinkResponse]
    catalogs: list[CatalogResponse]
