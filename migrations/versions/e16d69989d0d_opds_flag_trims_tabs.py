"""The OPDS 2.0 flag trims spaces and tabs around the media type, as the feed does.

`restrict=opds` is applied twice: the feed in Python (`is_opds2_media_type`), search with the
`has_opds_catalog` flag the rebuild stores. The rebuild trimmed with `trim()`, which removes spaces
only, so a type stored as "application/opds+json<TAB>; charset=utf-8" passed the feed's filter and
failed search's. Both now trim exactly optional whitespace, spaces and tabs (RFC 9110 §5.6.3), and
read a line break as part of the value, so neither matches one.

Only the media type expression of `refresh_catalog_search` changes; every row is rebuilt so the
stored flags follow. The downgrade puts back the expression `d4b8e2f61a90` wrote, and rebuilds.

Revision ID: e16d69989d0d
Revises: 06d8bef33737
Create Date: 2026-11-25
"""

# ruff: noqa: E501 - SQL blocks read better unwrapped

from collections.abc import Sequence

from alembic import op

revision: str = "e16d69989d0d"
down_revision: str | None = "06d8bef33737"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REBUILD_ALL = "SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs"

_TRIM_SPACES_AND_TABS = r"btrim(split_part(k.media_type, ';', 1), E' \t')"
_TRIM_SPACES = "trim(split_part(k.media_type, ';', 1))"


def _refresh(media_type: str) -> str:
    """`refresh_catalog_search` as `d4b8e2f61a90` wrote it, with *media_type* as the expression
    that reads the type of a link before the first `;`."""
    return (
        """
        CREATE OR REPLACE FUNCTION public.refresh_catalog_search(p_ids uuid[]) RETURNS void
        LANGUAGE sql VOLATILE
        SET search_path = pg_catalog, public
        AS $$
          -- Serialise rebuilds of one catalog. Two transactions adding different subdivisions
          -- would each rebuild without seeing the other's uncommitted row, and the last
          -- writer would drop one. FOR NO KEY UPDATE does not conflict with the key-share lock
          -- foreign keys take, so inserting a subdivision is not blocked by readers. Ordered
          -- by id so two multi-catalog rebuilds cannot deadlock. A separate statement, so the
          -- rebuild below takes a fresh snapshot after any wait.
          SELECT 1 FROM catalogs WHERE id = ANY (p_ids) ORDER BY id FOR NO KEY UPDATE;

          DELETE FROM catalog_search cs
           WHERE cs.catalog_id = ANY (p_ids)
             AND NOT EXISTS (SELECT 1 FROM catalogs c WHERE c.id = cs.catalog_id AND c.status = 'published');

          INSERT INTO catalog_search (catalog_id, title, city, places, has_opds_catalog, is_open, audiobook_only)
          SELECT c.id,
                 public.fold(c.title),
                 public.fold(coalesce(c.city, '')),
                 public.fold(concat_ws(' ', cn.names, sn.names)),
                 -- The same rules as `domain/restrictions.py`; the tests run both on the same data.
                 EXISTS (SELECT 1 FROM links k
                          WHERE k.catalog_id = c.id AND k.rel = 'catalog'
                            AND lower("""
        + media_type
        + """) = 'application/opds+json'),
                 EXISTS (SELECT 1 FROM catalog_kinds x WHERE x.catalog_id = c.id AND x.kind = 'open'),
                 EXISTS (SELECT 1 FROM catalog_publication_types p
                          WHERE p.catalog_id = c.id AND p.publication_type = 'audiobook')
                 AND NOT EXISTS (SELECT 1 FROM catalog_publication_types p
                                  WHERE p.catalog_id = c.id AND p.publication_type <> 'audiobook')
            FROM catalogs c
            LEFT JOIN LATERAL (
              SELECT string_agg(DISTINCT n.name, ' ') AS names
                FROM country_names n
               WHERE n.country_code = c.country_code
                 AND (n.language_tag = 'en'
                      OR EXISTS (SELECT 1 FROM country_languages l
                                  WHERE l.country_code = n.country_code AND l.language_tag = n.language_tag))
            ) cn ON true
            LEFT JOIN LATERAL (
              SELECT string_agg(DISTINCT n.name, ' ') AS names
                FROM catalog_subdivisions s
                JOIN subdivision_names n ON n.subdivision_code = s.subdivision_code
               WHERE s.catalog_id = c.id
                 AND (n.language_tag = 'en'
                      OR EXISTS (SELECT 1 FROM country_languages l
                                  WHERE l.country_code = left(n.subdivision_code, 2)
                                    AND l.language_tag = n.language_tag))
            ) sn ON true
           WHERE c.id = ANY (p_ids) AND c.status = 'published'

          ON CONFLICT (catalog_id) DO UPDATE
             SET title = EXCLUDED.title, city = EXCLUDED.city, places = EXCLUDED.places,
                 has_opds_catalog = EXCLUDED.has_opds_catalog, is_open = EXCLUDED.is_open,
                 audiobook_only = EXCLUDED.audiobook_only;
        $$;
        """
    )


def upgrade() -> None:
    op.execute(_refresh(_TRIM_SPACES_AND_TABS))
    op.execute(_REBUILD_ALL)


def downgrade() -> None:
    op.execute(_refresh(_TRIM_SPACES))
    op.execute(_REBUILD_ALL)
