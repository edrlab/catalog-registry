"""The admin API's reads: counts, the filtered catalog list, and what the filters can offer.

Each public method is one statement (R4): the list returns its page of catalogs with their places
and languages, and the total, in a single round trip. Counting happens in SQL.

The list's `ORDER BY` is picked from `_ORDER`, never built from the request, so a sort value cannot
reach the statement as text.
"""

# ruff: noqa: E501 - SQL kept on the lines the statement reads best on

import json
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.domain.admin_models import (
    AdminCatalogDetail,
    AdminCatalogLink,
    AdminCatalogPage,
    AdminCatalogQuery,
    AdminCatalogRow,
    AdminStats,
    AdminSubdivision,
    UsedCountry,
    UsedLanguage,
    UsedPlaces,
)
from registry.domain.enums import CatalogStatus
from registry.repositories.catalog_rows import fetch_rows

_STATS: Final = text(
    """
SELECT count(*) FILTER (WHERE status = 'draft') AS draft,
       count(*) FILTER (WHERE status = 'published') AS published,
       count(*) FILTER (WHERE status = 'unpublished') AS unpublished,
       count(*) FILTER (WHERE status = 'archived') AS archived,
       count(*) FILTER (WHERE status = 'published' AND recommended) AS recommended
  FROM catalogs
"""
)

#: Countries and languages that at least one catalog (of any status) uses. English country names,
#: English because the admin API is; the code is the fallback for a country without one.
_USED_PLACES: Final = text(
    """
SELECT
  (SELECT coalesce(jsonb_agg(jsonb_build_object('code', x.code, 'name', x.name, 'count', x.n)), '[]'::jsonb)
     FROM (SELECT c.country_code AS code,
                  coalesce((SELECT n.name FROM country_names n
                             WHERE n.country_code = c.country_code AND n.language_tag = 'en'
                             ORDER BY n.name LIMIT 1), c.country_code) AS name,
                  count(*) AS n
             FROM catalogs c WHERE c.country_code IS NOT NULL GROUP BY c.country_code) x) AS countries,
  (SELECT coalesce(jsonb_agg(jsonb_build_object('tag', y.tag, 'count', y.n)), '[]'::jsonb)
     FROM (SELECT language_tag AS tag, count(*) AS n
             FROM catalog_languages GROUP BY language_tag) y) AS languages
"""
)

_ORDER: Final = {
    "name": "m.sort_name, m.id",
    "-name": "m.sort_name DESC, m.id",
    "kind": "m.sort_kind NULLS LAST, m.sort_name, m.id",
    "status": "m.status, m.sort_name, m.id",
}

#: One catalog's list columns, `c` being the catalog. Shared by the list and the detail, so the
#: two can never describe a catalog differently.
_ROW_COLUMNS: Final = """
       c.id::text AS id, c.title AS name, c.status::text AS status, c.recommended, c.version,
       c.color::text AS color, c.coverage::text AS coverage, c.city, c.country_code,
       (SELECT lower(substring(k.href FROM '^https?://([^/:?#@]+)')) FROM links k
         WHERE k.catalog_id = c.id AND k.rel IN ('catalog', 'shelf')
         ORDER BY (k.rel = 'catalog') DESC, k.href LIMIT 1) AS host,
       (SELECT k.href FROM links k WHERE k.catalog_id = c.id AND k.rel = 'icon'
         ORDER BY k.href LIMIT 1) AS icon_url,
       (SELECT coalesce(array_agg(k.kind::text ORDER BY k.kind), '{}') FROM catalog_kinds k
         WHERE k.catalog_id = c.id) AS kinds,
       (SELECT n.name FROM country_names n
         WHERE n.country_code = c.country_code AND n.language_tag = 'en'
         ORDER BY n.name LIMIT 1) AS country_name,
       (SELECT n.name FROM catalog_subdivisions s
          JOIN subdivision_names n ON n.subdivision_code = s.subdivision_code AND n.language_tag = 'en'
         WHERE s.catalog_id = c.id ORDER BY s.subdivision_code, n.name LIMIT 1) AS subdivision,
       (SELECT coalesce(array_agg(l.language_tag ORDER BY l.language_tag), '{}')
          FROM catalog_languages l WHERE l.catalog_id = c.id) AS languages"""

#: `m` is the filtered set; the heavy columns are read only for the page. The first host in the
#: statement is the browse link's, the one a person recognises. The search text is split into
#: words and every word must occur in the folded title, host, city, or any-language country or
#: subdivision name; `public.fold` is the database's own folding (ADR-059).
_LIST: Final = """
WITH matched AS (
  SELECT c.id, c.status, lower(c.title) AS sort_name,
         (SELECT min(k.kind::text) FROM catalog_kinds k WHERE k.catalog_id = c.id) AS sort_kind
    FROM catalogs c
   WHERE c.status = ANY (CAST(:statuses AS catalog_status[]))
     AND (CAST(:kinds AS text[]) IS NULL OR EXISTS (
           SELECT 1 FROM catalog_kinds k
            WHERE k.catalog_id = c.id AND k.kind::text = ANY (CAST(:kinds AS text[]))))
     AND (CAST(:coverages AS text[]) IS NULL OR c.coverage::text = ANY (CAST(:coverages AS text[])))
     AND (CAST(:publication_types AS text[]) IS NULL OR EXISTS (
           SELECT 1 FROM catalog_publication_types p
            WHERE p.catalog_id = c.id AND p.publication_type::text = ANY (CAST(:publication_types AS text[]))))
     AND (CAST(:countries AS text[]) IS NULL OR c.country_code = ANY (CAST(:countries AS text[])))
     AND (CAST(:languages AS text[]) IS NULL OR EXISTS (
           SELECT 1 FROM catalog_languages l
            WHERE l.catalog_id = c.id AND l.language_tag = ANY (CAST(:languages AS text[]))))
     AND (CAST(:recommended AS boolean) IS NULL OR c.recommended = CAST(:recommended AS boolean))
     AND (CAST(:words AS text[]) IS NULL OR NOT EXISTS (
           SELECT 1
             FROM unnest(CAST(:words AS text[])) AS w(word),
                  LATERAL (SELECT public.fold(concat_ws(' ', c.title, c.city,
                    (SELECT substring(k.href FROM '^https?://([^/:?#@]+)') FROM links k
                      WHERE k.catalog_id = c.id AND k.rel IN ('catalog', 'shelf') LIMIT 1),
                    (SELECT string_agg(n.name, ' ') FROM country_names n
                      WHERE n.country_code = c.country_code),
                    (SELECT string_agg(n.name, ' ') FROM catalog_subdivisions s
                       JOIN subdivision_names n ON n.subdivision_code = s.subdivision_code
                      WHERE s.catalog_id = c.id))) AS hay) h
            WHERE strpos(h.hay, public.fold(w.word)) = 0))
), paged AS (
  SELECT m.id, row_number() OVER (ORDER BY {order}) AS pos
    FROM matched m
   ORDER BY pos
   LIMIT :limit OFFSET :offset
)
SELECT (SELECT count(*) FROM matched) AS total,
{columns}
  FROM (SELECT 1) AS one
  LEFT JOIN paged ON true
  LEFT JOIN catalogs c ON c.id = paged.id
 ORDER BY paged.pos
"""
_LISTS: Final = {
    sort: text(_LIST.format(order=order, columns=_ROW_COLUMNS)) for sort, order in _ORDER.items()
}

#: One catalog with everything the detail page shows, in one statement (R4). A malformed id never
#: reaches it: the service answers 404 first.
_DETAIL: Final = text(
    "SELECT"
    + _ROW_COLUMNS
    + """,
       c.description, c.created_at, c.updated_at, c.published_at,
       c.submitter_name, c.submitter_email,
       (SELECT coalesce(array_agg(p.publication_type::text ORDER BY p.publication_type::text), '{}')
          FROM catalog_publication_types p WHERE p.catalog_id = c.id) AS publication_types,
       (SELECT coalesce(jsonb_agg(jsonb_build_object(
                 'code', s.subdivision_code,
                 'name', (SELECT n.name FROM subdivision_names n
                           WHERE n.subdivision_code = s.subdivision_code AND n.language_tag = 'en'
                           ORDER BY n.name LIMIT 1))
               ORDER BY s.subdivision_code), '[]'::jsonb)
          FROM catalog_subdivisions s WHERE s.catalog_id = c.id) AS subdivision_list,
       (SELECT coalesce(jsonb_agg(jsonb_build_object(
                 'rel', k.rel::text, 'href', k.href, 'type', k.media_type, 'title', k.title,
                 'templated', k.templated, 'authentication', k.authentication)
               ORDER BY k.rel::text, k.href), '[]'::jsonb)
          FROM links k WHERE k.catalog_id = c.id) AS links
  FROM catalogs c
 WHERE c.id = CAST(:catalog_id AS uuid)
"""
)


def _array_or_none(values: list[str] | Any) -> list[str] | None:
    """An empty filter means "no filter", which SQL sees as NULL."""
    return list(values) if values else None


def _json(value: Any) -> Any:
    """asyncpg hands jsonb back as text."""
    return json.loads(value) if isinstance(value, str) else value


def _build_row(row: Any) -> AdminCatalogRow:
    return AdminCatalogRow(
        id=row["id"],
        name=row["name"],
        host=row["host"],
        color=row["color"],
        icon_url=row["icon_url"],
        kinds=list(row["kinds"]),
        coverage=row["coverage"],
        city=row["city"],
        subdivision=row["subdivision"],
        country_code=row["country_code"],
        country_name=row["country_name"],
        languages=list(row["languages"]),
        status=CatalogStatus(row["status"]),
        recommended=row["recommended"],
        version=row["version"],
    )


class AdminRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_admin_stats(self) -> AdminStats:
        (row,) = await fetch_rows(self._session, _STATS, {})
        return AdminStats(
            by_status={status.value: row[status.value] for status in CatalogStatus},
            recommended=row["recommended"],
        )

    async def fetch_used_places(self) -> UsedPlaces:
        (row,) = await fetch_rows(self._session, _USED_PLACES, {})
        countries, languages = _json(row["countries"]), _json(row["languages"])
        return UsedPlaces(
            countries=[UsedCountry(c["code"], c["name"], c["count"]) for c in countries],
            languages=[UsedLanguage(item["tag"], item["count"]) for item in languages],
        )

    async def fetch_admin_catalogs(self, query: AdminCatalogQuery) -> AdminCatalogPage:
        rows = await fetch_rows(
            self._session,
            _LISTS[query.sort],
            {
                "statuses": [status.value for status in query.statuses],
                "kinds": _array_or_none(query.kinds),
                "coverages": _array_or_none(query.coverages),
                "publication_types": _array_or_none(query.publication_types),
                "countries": _array_or_none(query.countries),
                "languages": _array_or_none(query.languages),
                "recommended": query.recommended,
                "words": _array_or_none(query.words),
                "limit": query.per_page,
                "offset": (query.page - 1) * query.per_page,
            },
        )
        # One row always comes back, so the total survives a page past the end; its catalog
        # columns are NULL then.
        return AdminCatalogPage(
            total=rows[0]["total"],
            rows=[_build_row(row) for row in rows if row["id"] is not None],
        )

    async def fetch_admin_catalog(self, catalog_id: str) -> AdminCatalogDetail | None:
        rows = await fetch_rows(self._session, _DETAIL, {"catalog_id": catalog_id})
        if not rows:
            return None
        row = rows[0]
        return AdminCatalogDetail(
            row=_build_row(row),
            description=row["description"],
            publication_types=list(row["publication_types"]),
            subdivisions=[
                AdminSubdivision(code=item["code"], name=item["name"])
                for item in _json(row["subdivision_list"])
            ],
            links=[
                AdminCatalogLink(
                    rel=item["rel"],
                    href=item["href"],
                    media_type=item["type"],
                    title=item["title"],
                    templated=item["templated"],
                    authentication=item["authentication"],
                )
                for item in _json(row["links"])
            ],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            published_at=row["published_at"],
            submitter_name=row["submitter_name"],
            submitter_email=row["submitter_email"],
        )
