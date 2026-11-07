"""One catalog document to ORM rows, shared by the seed, `add` and the admin API's writes.

The document is the seed's input shape (`{metadata, links}`), already validated: the schema, then
`domain/catalog_document.py`. Building the rows in one place is what makes "the same write path"
true rather than two mappings that drift.
"""

import uuid
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from registry.db.models.catalog import (
    Catalog,
    CatalogKindRow,
    CatalogLanguageRow,
    CatalogPublicationTypeRow,
    CatalogSubdivisionRow,
)
from registry.db.models.link import Link
from registry.domain.catalog_document import link_rels
from registry.domain.enums import (
    CatalogColor,
    CatalogKind,
    CatalogStatus,
    CoverageScope,
    PublicationType,
)
from registry.domain.language import normalise_language_tag


def build_catalog(document: dict[str, Any], catalog_id: uuid.UUID, *, recommended: bool) -> Catalog:
    """*catalog_id* comes from `resolve_catalog_id`. The caller resolves it, because it is also
    what `resolve_existing_catalog` looks the row up by."""
    metadata = document["metadata"]

    return Catalog(
        id=catalog_id,
        status=CatalogStatus.PUBLISHED,
        recommended=recommended,
        title=metadata["title"],
        description=metadata.get("description"),
        color=CatalogColor(metadata.get("color", CatalogColor.GRAY.value)),
        # No case folding. ISO 3166-1 is officially uppercase and the input schema enforces it,
        # so a lowercase code is rejected in validation rather than quietly corrected here.
        country_code=metadata.get("country"),
        city=metadata.get("city"),
        # Absent means NULL, not `global`. Storing an undeclared coverage as
        # `global` would assert worldwide reach on the catalog's behalf.
        coverage=(CoverageScope(metadata["coverage"]) if metadata.get("coverage") else None),
        # ck_catalogs_published_when_published: a published catalog must carry a publication date.
        published_at=None,
        kinds=[CatalogKindRow(kind=CatalogKind(value)) for value in metadata["kind"]],
        publication_types=[
            CatalogPublicationTypeRow(publication_type=PublicationType(value))
            for value in metadata.get("publicationTypes", ())
        ],
        # Languages are the one field still folded, and deliberately: BCP-47 writes a language
        # subtag lowercase and a region subtag uppercase, `Accept-Language` arrives lowercase, and
        # the published pattern accepts either case because the spec does. Country and
        # subdivision codes are *not* folded; their patterns admit uppercase only.
        languages=[
            CatalogLanguageRow(language_tag=normalise_language_tag(value))
            for value in metadata.get("supportedLanguages", ())
        ],
        # Uppercase already, for the same reason as `country_code` above.
        subdivisions=[
            CatalogSubdivisionRow(subdivision_code=value)
            for value in metadata.get("subdivisions", ())
        ],
        # One row per rel: a link declaring `["catalog", "start"]` is two relations to the
        # same href, and the wire contract carries a single rel per link.
        links=[
            Link(
                href=link["href"],
                media_type=link.get("type"),
                rel=rel,
                templated=bool(link.get("templated", False)),
                title=link.get("title"),
            )
            for link in document["links"]
            for rel in link_rels(link)
        ],
    )


async def persist_catalog_contents(
    session: AsyncSession, existing: Catalog, built: Catalog
) -> None:
    """Give *existing* the editorial fields and child rows of *built*, in place. Status, the
    recommended flag, dates and version are the caller's.

    The child collections are replaced wholesale rather than diffed: the document is the whole
    truth for this catalog, and a diff is more code for the same result.
    """
    for model, column in (
        (CatalogKindRow, CatalogKindRow.catalog_id),
        (CatalogPublicationTypeRow, CatalogPublicationTypeRow.catalog_id),
        (CatalogLanguageRow, CatalogLanguageRow.catalog_id),
        (CatalogSubdivisionRow, CatalogSubdivisionRow.catalog_id),
        (Link, Link.catalog_id),
    ):
        await session.execute(delete(model).where(column == existing.id))

    existing.title = built.title
    existing.description = built.description
    existing.color = built.color
    existing.country_code = built.country_code
    existing.city = built.city
    existing.coverage = built.coverage
    existing.kinds = built.kinds
    existing.publication_types = built.publication_types
    existing.languages = built.languages
    existing.subdivisions = built.subdivisions
    existing.links = built.links


def build_document_from_catalog(catalog: Catalog) -> dict[str, Any]:
    """The catalog document *catalog* would be built from: the inverse of `build_catalog`, so an
    edit can be applied to it and the whole result checked as a new one would be. Needs the
    collections loaded (`EAGER_COLLECTIONS`)."""
    metadata: dict[str, Any] = {
        "title": catalog.title,
        "kind": [row.kind.value for row in catalog.kinds],
        "color": catalog.color.value,
        "subdivisions": [row.subdivision_code for row in catalog.subdivisions],
        "supportedLanguages": [row.language_tag for row in catalog.languages],
        "publicationTypes": [row.publication_type.value for row in catalog.publication_types],
    }
    optional = {
        "description": catalog.description,
        "coverage": catalog.coverage.value if catalog.coverage else None,
        "country": catalog.country_code,
        "city": catalog.city,
    }
    metadata |= {key: value for key, value in optional.items() if value is not None}
    links = []
    for link in catalog.links:
        item: dict[str, Any] = {"rel": link.rel.value, "href": link.href}
        if link.media_type is not None:
            item["type"] = link.media_type
        if link.title is not None:
            item["title"] = link.title
        if link.templated:
            item["templated"] = True
        links.append(item)
    return {"metadata": metadata, "links": links}
