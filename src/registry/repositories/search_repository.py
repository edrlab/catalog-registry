"""Catalog search on TIN (PlanetScale's BM25 index): one statement for the matches, the page and
every child of it.

The statement's last SELECT is widened: instead of returning ids for a
second query to load, it returns each catalog's own columns, its kinds, publication types, languages
and subdivisions as arrays, and its links as one JSON array. One statement is one round trip to the
database whatever the page size, which is what keeps search under 100 ms when the database is a
region away (docs/search.md). It is still no N+1: there is no per-row query at all.

Rows become plain `CatalogView` values with the attribute names `render_catalog` already
reads, so the renderer, and with it the whitelist projection, is untouched. Parameters are
bound with explicit array types because asyncpg will not infer them, and the `CAST`s make the
same types explicit to PostgreSQL.
"""

from typing import Final

from sqlalchemy import TEXT, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from registry.domain.restrictions import NO_RESTRICTIONS, Restrictions
from registry.domain.search_query import MAX_RESULTS, ParsedQuery
from registry.repositories.catalog_rows import (
    CATALOG_COLUMNS,
    build_catalog_from_row,
    fetch_rows,
)
from registry.repositories.protocols import SearchPage

#: BM25 boost per field, as TINQL `^` factors: a title match outweighs a city match, which outweighs
#: a country or subdivision name (places, boost 1). Changing one needs no reindex.
TITLE_BOOST: Final = 5
CITY_BOOST: Final = 2

#: The statement timeout that turns a runaway search into a 503.
SEARCH_STATEMENT_TIMEOUT_MS: Final = 1000

#: Sent once per connection, in the startup packet, to the search pool only (`build_read_engine`).
#: Read-only because search must never write.
SEARCH_CONNECTION_SETTINGS: Final = {
    "statement_timeout": str(SEARCH_STATEMENT_TIMEOUT_MS),
    "default_transaction_read_only": "on",
}

#: `registry_tinql` builds the query from the folded words (any word matches; typos and the last
#: word's prefix too). `tin.full_score`, not `tin.score`: that one ignores words in more than
#: 10% of rows, so a broad word such as `bibliotheque` or `france` would score every catalog 0.
#: A typo or prefix match scores 0 and so follows every exact match, newest first.
_SEARCH_SQL: Final = (
    f"""
WITH q AS (
  SELECT public.registry_tinql(CAST(:positives AS text[]), true)  AS match_q,
         public.registry_tinql(CAST(:negatives AS text[]), false) AS exclude_q
),
hits AS (
  SELECT cs.catalog_id, tin.full_score(cs.ctid) AS score, c.created_at
    FROM catalog_search cs
    CROSS JOIN q
    JOIN catalogs c ON c.id = cs.catalog_id AND c.status = 'published'
   WHERE (cs.title ==> ('(' || q.match_q || ')^{TITLE_BOOST}')
          OR cs.city ==> ('(' || q.match_q || ')^{CITY_BOOST}')
          OR cs.places ==> q.match_q)
     AND (q.exclude_q IS NULL
          OR NOT (cs.title ==> q.exclude_q OR cs.city ==> q.exclude_q OR cs.places ==> q.exclude_q))
     AND (NOT CAST(:opds AS boolean) OR cs.has_opds_catalog)
     AND (NOT CAST(:no_open AS boolean) OR NOT cs.is_open)
     AND (NOT CAST(:no_audiobook AS boolean) OR NOT cs.audiobook_only)
)
SELECT t.total, p.score, """
    + CATALOG_COLUMNS
    + """
  FROM (SELECT count(*) AS total FROM (SELECT 1 FROM hits LIMIT :count_cap) capped) t
  LEFT JOIN LATERAL (
        SELECT catalog_id, score,
               row_number() OVER (ORDER BY score DESC, created_at DESC, catalog_id) AS rn
          FROM hits ORDER BY rn LIMIT :limit OFFSET :offset) p ON true
  LEFT JOIN catalogs c ON c.id = p.catalog_id
 ORDER BY p.rn
"""
)

_SEARCH: Final = text(_SEARCH_SQL).bindparams(
    bindparam("positives", type_=ARRAY(TEXT)),
    bindparam("negatives", type_=ARRAY(TEXT)),
)


class CatalogSearchRepository:
    """Satisfies `CatalogSearcher`. Runs on the search pool (`build_read_engine`), which carries
    the timeout and the read-only setting on the connection."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_catalogs(
        self,
        query: ParsedQuery,
        *,
        limit: int,
        offset: int,
        restrictions: Restrictions = NO_RESTRICTIONS,
    ) -> SearchPage:
        """One page of matches in rank order, with the total (at most `MAX_RESULTS`), in one
        statement."""
        params = {
            "positives": list(query.positives),
            "negatives": list(query.negatives),
            "limit": limit,
            "offset": offset,
            "opds": restrictions.opds,
            "count_cap": MAX_RESULTS,
            "no_open": restrictions.no_open,
            "no_audiobook": restrictions.no_audiobook,
        }
        rows = await fetch_rows(self._session, _SEARCH, params)
        total = rows[0]["total"]  # the statement always returns at least one row
        return SearchPage(
            total=total,
            catalogs=tuple(build_catalog_from_row(row) for row in rows if row["catalog_id"]),
        )
