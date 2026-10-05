"""Catalog search: one statement for the matches, one query to load the page (ADR-051).

The statement is the tested SQL from the reference implementation, kept as a module constant
rather than read from a file at runtime. Parameters are bound with explicit array types because
asyncpg will not infer them, and the `CAST`s make the same types explicit to PostgreSQL.
"""

from typing import Final

from sqlalchemy import REAL, TEXT, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from registry.core.errors import SearchTimeoutError
from registry.db.models.catalog import Catalog
from registry.domain.enums import CatalogStatus
from registry.domain.search_query import ParsedQuery
from registry.repositories.catalog_repository import EAGER_COLLECTIONS
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
#: per transaction by `read_only_transaction`, so the server's own setting stays untouched.
WORD_SIMILARITY_THRESHOLD: Final = 0.5

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
SELECT t.total, p.catalog_id, p.tier, p.score
  FROM (SELECT count(*) AS total FROM hits) t
  LEFT JOIN LATERAL (
        SELECT catalog_id, tier, score FROM hits
         ORDER BY tier, score DESC, created_at DESC, catalog_id
         LIMIT :limit OFFSET :offset) p ON true
"""

_SEARCH: Final = text(_SEARCH_SQL).bindparams(
    bindparam("positives", type_=ARRAY(TEXT)),
    bindparam("negatives", type_=ARRAY(TEXT)),
    bindparam("weights", type_=ARRAY(REAL)),
)


class CatalogSearchRepository:
    """Satisfies `CatalogSearcher`. Runs inside `read_only_transaction`, opened by the caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_catalogs(self, query: ParsedQuery, *, limit: int, offset: int) -> SearchPage:
        """One page of matches in rank order, with the total across all pages.

        A timeout is translated here because `api` may not import SQLAlchemy.
        """
        params = {
            "positives": list(query.positives),
            "negatives": list(query.negatives),
            "trigram_text": query.trigram_text,
            "weights": list(RANK_WEIGHTS),
            "limit": limit,
            "offset": offset,
        }
        try:
            rows = (await self._session.execute(_SEARCH, params)).all()
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == _QUERY_CANCELED:
                raise SearchTimeoutError(
                    "the search took too long, try a more specific query"
                ) from error
            raise
        total = rows[0].total  # the statement always returns at least one row
        ids = [row.catalog_id for row in rows if row.catalog_id is not None]
        if not ids:
            return SearchPage(total=total, catalogs=())
        statement = (
            select(Catalog)
            .where(Catalog.id.in_(ids), Catalog.status == CatalogStatus.ACTIVE)
            .options(*EAGER_COLLECTIONS)
        )
        by_id = {c.id: c for c in (await self._session.scalars(statement)).all()}
        return SearchPage(total=total, catalogs=tuple(by_id[i] for i in ids if i in by_id))
