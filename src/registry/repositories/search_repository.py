"""Catalog search: one statement for the matches, the page and every child of it (ADR-051, ADR-060).

The statement is the tested search SQL with its last SELECT widened: instead of returning ids for a
second query to load, it returns each catalog's own columns, its kinds, publication types, languages
and subdivisions as arrays, and its links as one JSON array. One statement is one round trip to the
database whatever the page size, which is what keeps search under 100 ms when the database is a
region away (docs/search.md). It is still no N+1 (R4): there is no per-row query at all.

Rows become the same `Catalog` objects `render_catalog` already reads, so the renderer, and with it
the whitelist projection (R3), is untouched. Parameters are bound with explicit array types because
asyncpg will not infer them, and the `CAST`s make the same types explicit to PostgreSQL.
"""

import json
from typing import Any, Final

from sqlalchemy import REAL, TEXT, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from registry.core.errors import SearchTimeoutError
from registry.db.models.catalog import (
    Catalog,
    CatalogKindRow,
    CatalogLanguageRow,
    CatalogPublicationTypeRow,
    CatalogSubdivisionRow,
)
from registry.db.models.link import Link
from registry.domain.enums import (
    CatalogColor,
    CatalogKind,
    CoverageScope,
    LinkRel,
    PublicationType,
)
from registry.domain.search_query import ParsedQuery
from registry.repositories.protocols import SearchPage

#: `ts_rank` weights in PostgreSQL's order {D, C, B, A}. Labels are fixed per field when the
#: document is built (A title, B country names, C subdivision names and city, D unused), so the
#: ranking policy lives here and changes with no rebuild (ADR-052). Current: the Notion order,
#: title, then country, then subdivisions. The meeting-notes order would be (0.1, 0.4, 0.2, 1.0).
#: Open question Q3.
RANK_WEIGHTS: Final = (0.1, 0.2, 0.4, 1.0)

#: How alike a typed word and a stored one must be for the trigram half to match (pg_trgm
#: `word_similarity`, used by `<%`). PostgreSQL's default is 0.6. Measured on 4 and 6 October 2026
#: (`tests/integration/test_search_fuzzy.py`): 0.6 finds 24% of swapped-neighbour typos and 61%
#: of replaced letters; 0.5 finds 66% and 90% and lets one extra unrelated word through. Applied
#: on the search pool's connections (`SEARCH_CONNECTION_SETTINGS`), so the server's own setting
#: stays untouched and nothing reaches other connections.
WORD_SIMILARITY_THRESHOLD: Final = 0.5

#: The statement timeout that turns a runaway search into a 503 (ADR-046, ADR-058).
SEARCH_STATEMENT_TIMEOUT_MS: Final = 1000

#: Sent once per connection, in the startup packet, to the search pool only (`build_search_engine`).
#: Read-only because search must never write; the threshold because `<%` reads it.
SEARCH_CONNECTION_SETTINGS: Final = {
    "statement_timeout": str(SEARCH_STATEMENT_TIMEOUT_MS),
    "default_transaction_read_only": "on",
    "pg_trgm.word_similarity_threshold": str(WORD_SIMILARITY_THRESHOLD),
}

#: SQLSTATE `query_canceled`, what `statement_timeout` raises.
_QUERY_CANCELED: Final = "57014"

_SEARCH_SQL: Final = """
WITH q AS (
  SELECT public.registry_query(CAST(:positives AS text[]), CAST(:negatives AS text[])) AS match_q,
         public.registry_query(CAST(:positives AS text[]), CAST('{}' AS text[]))      AS rank_q,
         public.registry_query(CAST(:negatives AS text[]), CAST('{}' AS text[]))      AS exclude_q,
         public.fold(CAST(:trigram_text AS text))                                      AS trigram_q
),
word_hits AS (
  SELECT cs.catalog_id, 1 AS tier,
         ts_rank(CAST(:weights AS real[]), cs.document, q.rank_q) AS score
    FROM catalog_search cs, q
   WHERE cs.document @@ q.match_q
),
trigram_hits AS (
  SELECT cs.catalog_id, 2 AS tier, word_similarity(q.trigram_q, cs.names) AS score
    FROM catalog_search cs, q
   WHERE q.trigram_q <> ''
     AND q.trigram_q <% cs.names
     AND (q.exclude_q IS NULL OR NOT cs.document @@ q.exclude_q)
     AND NOT EXISTS (SELECT 1 FROM word_hits w WHERE w.catalog_id = cs.catalog_id)
),
hits AS (
  SELECT h.catalog_id, h.tier, h.score, c.created_at
    FROM (SELECT * FROM word_hits UNION ALL SELECT * FROM trigram_hits) h
    JOIN catalogs c ON c.id = h.catalog_id AND c.status = 'active'
)
SELECT t.total, p.tier, p.score, c.id AS catalog_id, c.title, c.description,
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
     FROM links k WHERE k.catalog_id = c.id) AS links
  FROM (SELECT count(*) AS total FROM hits) t
  LEFT JOIN LATERAL (
        SELECT catalog_id, tier, score,
               row_number() OVER (ORDER BY tier, score DESC, created_at DESC, catalog_id) AS rn
          FROM hits ORDER BY rn LIMIT :limit OFFSET :offset) p ON true
  LEFT JOIN catalogs c ON c.id = p.catalog_id
 ORDER BY p.rn
"""

_SEARCH: Final = text(_SEARCH_SQL).bindparams(
    bindparam("positives", type_=ARRAY(TEXT)),
    bindparam("negatives", type_=ARRAY(TEXT)),
    bindparam("weights", type_=ARRAY(REAL)),
)


def build_catalog_from_row(row: Any) -> Catalog:
    """A search row as the `Catalog` the renderer reads. Not attached to a session.

    Only what `render_catalog` projects is set. A link's `rel` is a database enum, hence `LinkRel`.
    """
    links = row["links"]
    if isinstance(links, str):  # asyncpg hands jsonb back as text
        links = json.loads(links)
    return Catalog(
        id=row["catalog_id"],
        title=row["title"],
        description=row["description"],
        color=CatalogColor(row["color"]),
        country_code=row["country_code"],
        city=row["city"],
        coverage=CoverageScope(row["coverage"]) if row["coverage"] else None,
        kinds=[CatalogKindRow(kind=CatalogKind(value)) for value in row["kinds"]],
        publication_types=[
            CatalogPublicationTypeRow(publication_type=PublicationType(value))
            for value in row["publication_types"]
        ],
        languages=[CatalogLanguageRow(language_tag=value) for value in row["languages"]],
        subdivisions=[CatalogSubdivisionRow(subdivision_code=v) for v in row["subdivisions"]],
        links=[
            Link(
                href=link["href"],
                media_type=link["type"],
                rel=LinkRel(link["rel"]),
                templated=bool(link["templated"]),
                title=link["title"],
            )
            for link in links
        ],
    )


class CatalogSearchRepository:
    """Satisfies `CatalogSearcher`. Runs on the search pool (`build_search_engine`), which carries
    the timeout, the read-only setting and the typo threshold on the connection."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_catalogs(self, query: ParsedQuery, *, limit: int, offset: int) -> SearchPage:
        """One page of matches in rank order, with the total across all pages, in one statement.

        A timeout is translated here because `api` may not import SQLAlchemy. A connection the
        database dropped while it sat idle is retried once: the statement only reads, so repeating
        it is safe.
        """
        params = {
            "positives": list(query.positives),
            "negatives": list(query.negatives),
            "trigram_text": query.trigram_text,
            "weights": list(RANK_WEIGHTS),
            "limit": limit,
            "offset": offset,
        }
        for attempt in (1, 2):
            try:
                rows = (await self._session.execute(_SEARCH, params)).mappings().all()
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) == _QUERY_CANCELED:
                    raise SearchTimeoutError(
                        "the search took too long, try a more specific query"
                    ) from error
                if error.connection_invalidated and attempt == 1:
                    await self._session.rollback()
                    continue
                raise
            break
        total = rows[0]["total"]  # the statement always returns at least one row
        return SearchPage(
            total=total,
            catalogs=tuple(build_catalog_from_row(row) for row in rows if row["catalog_id"]),
        )
