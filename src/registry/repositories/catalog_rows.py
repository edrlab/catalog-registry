"""What every public read shares: the columns of a catalog and its children in one SELECT list, the
mapping from such a row to the `CatalogView` the renderer reads, and the one place a read statement
is run (ADR-060, ADR-062).

A catalog's kinds, publication types, languages and subdivisions come back as arrays and its links
as one JSON array, each from a correlated sub-select on an indexed `catalog_id`. So a page of
catalogs is one statement, one round trip, whatever its size: no N+1 (R4), and no row multiplication
from joining five collections at once.
"""

import json
from typing import Any, Final

from sqlalchemy import TextClause
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from registry.core.errors import ReadTimeoutError
from registry.domain.catalog_view import (
    CatalogView,
    KindRow,
    LanguageRow,
    LinkRow,
    PublicationTypeRow,
    SubdivisionRow,
)
from registry.domain.enums import (
    CatalogColor,
    CatalogKind,
    CoverageScope,
    LinkRel,
    PublicationType,
)

#: A SELECT list. The alias `c` is `catalogs`. Explicit columns: a new column cannot leak (R3).
CATALOG_COLUMNS: Final = """c.id AS catalog_id, c.created_at, c.title, c.description,
       c.color::text AS color, c.country_code, c.city, c.coverage::text AS coverage,
  (SELECT coalesce(array_agg(k.kind::text), '{}') FROM catalog_kinds k
    WHERE k.catalog_id = c.id) AS kinds,
  (SELECT coalesce(array_agg(x.publication_type::text), '{}') FROM catalog_publication_types x
    WHERE x.catalog_id = c.id) AS publication_types,
  (SELECT coalesce(array_agg(l.language_tag), '{}') FROM catalog_languages l
    WHERE l.catalog_id = c.id) AS languages,
  (SELECT coalesce(array_agg(s.subdivision_code), '{}') FROM catalog_subdivisions s
    WHERE s.catalog_id = c.id) AS subdivisions,
  (SELECT coalesce(jsonb_agg(jsonb_build_object('href', k.href, 'type', k.media_type,
                                                 'rel', k.rel::text, 'templated', k.templated,
                                                 'title', k.title)), '[]'::jsonb)
     FROM links k WHERE k.catalog_id = c.id) AS links"""

#: SQLSTATE `query_canceled`, what `statement_timeout` raises.
_QUERY_CANCELED: Final = "57014"


def build_catalog_from_row(row: Any) -> CatalogView:
    """A row with `CATALOG_COLUMNS` as the plain `CatalogView` the renderer reads.

    Only what `render_catalog` projects, and `created_at` for the feed's ordering, is set. A link's
    `rel` is a database enum, hence `LinkRel`.
    """
    links = row["links"]
    if isinstance(links, str):  # asyncpg hands jsonb back as text
        links = json.loads(links)
    return CatalogView(
        id=row["catalog_id"],
        # Not rendered (the renderer is a whitelist, R3): the feed's language sort ties on it.
        created_at=row["created_at"],
        title=row["title"],
        description=row["description"],
        color=CatalogColor(row["color"]),
        country_code=row["country_code"],
        city=row["city"],
        coverage=CoverageScope(row["coverage"]) if row["coverage"] else None,
        kinds=[KindRow(CatalogKind(value)) for value in row["kinds"]],
        publication_types=[
            PublicationTypeRow(PublicationType(v)) for v in row["publication_types"]
        ],
        languages=[LanguageRow(value) for value in row["languages"]],
        subdivisions=[SubdivisionRow(value) for value in row["subdivisions"]],
        links=[
            LinkRow(
                link["href"],
                link["type"],
                LinkRel(link["rel"]),
                bool(link["templated"]),
                link["title"],
            )
            for link in links
        ],
    )


async def _run(session: AsyncSession, statement: TextClause, params: dict[str, Any]) -> list[Any]:
    try:
        return list((await session.execute(statement, params)).mappings().all())
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == _QUERY_CANCELED:
            raise ReadTimeoutError("the read took too long, try again") from error
        raise


async def fetch_rows(
    session: AsyncSession, statement: TextClause, params: dict[str, Any]
) -> list[Any]:
    """Run one read statement and return its rows.

    A timeout (`statement_timeout` on the connection) becomes a `ReadTimeoutError` here, because
    `api` may not import SQLAlchemy. A connection the database dropped while it sat idle is retried
    once; the statement only reads, so repeating it is safe.
    """
    try:
        return await _run(session, statement, params)
    except DBAPIError as error:
        if not error.connection_invalidated:
            raise
        await session.rollback()
        return await _run(session, statement, params)
