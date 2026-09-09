"""Catalog row → OPDS catalog document.

**Whitelist projection.** This builds the document field by field. It never iterates the
model's attributes, so adding a column cannot leak it into a response, and
`catalog.schema.json` sets `additionalProperties: false` on `metadata`, which makes a leak a
contract-test failure rather than a quiet disclosure.

`submitter_name`, `submitter_email`, `status`, `recommended`, `created_at`, `updated_at` and
`published_at` are internal. They have no path to this function's output.
"""

import uuid
from typing import Any

from registry.core.constants import OPDS_CATALOG_MEDIA_TYPE
from registry.db.models.catalog import Catalog
from registry.db.models.link import Link
from registry.domain.enums import LinkRel
from registry.domain.links import order_links


def render_link(link: Link) -> dict[str, Any]:
    """Key order matches `LinkResponse`, so a stored link and a synthesised one look alike."""
    document: dict[str, Any] = {"href": link.href}
    if link.media_type:
        document["type"] = link.media_type
    document["rel"] = link.rel.value
    if link.templated:
        document["templated"] = True
    if link.title:
        document["title"] = link.title
    return document


def build_catalog_self_link(catalog_id: uuid.UUID, *, base_url: str) -> dict[str, Any]:
    """`self` points at this registry, so the registry synthesises it.

    Seed input carries no `self` link: it would mean asking whoever authored the catalog to
    invent a URL for a registry that did not exist yet.
    """
    return {
        "href": f"{base_url.rstrip('/')}/catalogs/{catalog_id}",
        "type": OPDS_CATALOG_MEDIA_TYPE,
        "rel": LinkRel.SELF.value,
    }


def render_catalog(catalog: Catalog, *, base_url: str) -> dict[str, Any]:
    """Every collection is sorted before emission, so the output is deterministic."""
    metadata: dict[str, Any] = {
        "title": catalog.title,
        "kind": sorted(row.kind.value for row in catalog.kinds),
    }
    if catalog.description:
        metadata["description"] = catalog.description
    metadata["color"] = catalog.color.value
    if catalog.languages:
        metadata["supportedLanguages"] = sorted(row.language_tag for row in catalog.languages)
    if catalog.publication_types:
        metadata["publicationTypes"] = sorted(
            row.publication_type.value for row in catalog.publication_types
        )
    if catalog.country_code:
        metadata["country"] = catalog.country_code.upper()
    if catalog.subdivisions:
        metadata["subdivisions"] = sorted(row.subdivision_code for row in catalog.subdivisions)
    if catalog.city:
        metadata["city"] = catalog.city
    # Omitted when not declared. Emitting `global` here would re-introduce exactly
    # the claim the nullable column exists to avoid.
    if catalog.coverage is not None:
        metadata["coverage"] = catalog.coverage.value

    stored = [
        render_link(link) for link in order_links(catalog.links, rel_of=lambda link: link.rel)
    ]
    return {
        "metadata": metadata,
        # `self` first, matching LINK_REL_PRIORITY, and synthesised rather than stored.
        "links": [build_catalog_self_link(catalog.id, base_url=base_url), *stored],
    }
