"""Search document labels: title A, city B, countries and subdivisions C.

Hadrien's order (2026-10-14). Until now the city shared label C with the subdivisions and the
country names had B. A city is more specific than a country, so it now ranks above it:

| label | before                      | after                           |
|-------|-----------------------------|---------------------------------|
| A     | title                       | title                           |
| B     | country names               | city                            |
| C     | subdivision names and city  | country names and subdivisions  |

Only the labels inside the stored `document` change. `RANK_WEIGHTS` is untouched, so the weights
follow the labels (A 1.0, B 0.4, C 0.2) and a city match now scores 0.4 where it scored 0.2.
`names`, the trigram half, does not carry labels and is unchanged.

The function is replaced and every row is rebuilt with it, because the labels are baked into the
stored text. The downgrade puts the old function back and rebuilds again. No schema change.

Revision ID: 6d2a91c5e7b3
Revises: 2eb49a8f4664
Create Date: 2026-10-14
"""

# ruff: noqa: E501 - the SQL blocks are the tested reference text, kept verbatim

from collections.abc import Sequence

from alembic import op

revision: str = "6d2a91c5e7b3"
down_revision: str | None = "2eb49a8f4664"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REBUILD_ALL = "SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs"


def upgrade() -> None:
    op.execute(
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
             AND NOT EXISTS (SELECT 1 FROM catalogs c WHERE c.id = cs.catalog_id AND c.status = 'active');

          INSERT INTO catalog_search (catalog_id, document, names)
          SELECT c.id,
                 setweight(to_tsvector('public.registry_simple', c.title), 'A')
              || setweight(to_tsvector('public.registry_simple', coalesce(c.city, '')), 'B')
              || setweight(to_tsvector('public.registry_simple', concat_ws(' ', cn.names, sn.names)), 'C'),
                 public.fold(concat_ws(' ', c.title, c.city, sn.names, cn.names))
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
           WHERE c.id = ANY (p_ids) AND c.status = 'active'
          ON CONFLICT (catalog_id) DO UPDATE
             SET document = EXCLUDED.document, names = EXCLUDED.names;
        $$;
        """
    )
    op.execute(_REBUILD_ALL)


def downgrade() -> None:
    op.execute(
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
             AND NOT EXISTS (SELECT 1 FROM catalogs c WHERE c.id = cs.catalog_id AND c.status = 'active');

          INSERT INTO catalog_search (catalog_id, document, names)
          SELECT c.id,
                 setweight(to_tsvector('public.registry_simple', c.title), 'A')
              || setweight(to_tsvector('public.registry_simple', coalesce(cn.names, '')), 'B')
              || setweight(to_tsvector('public.registry_simple', concat_ws(' ', sn.names, c.city)), 'C'),
                 public.fold(concat_ws(' ', c.title, c.city, sn.names, cn.names))
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
           WHERE c.id = ANY (p_ids) AND c.status = 'active'
          ON CONFLICT (catalog_id) DO UPDATE
             SET document = EXCLUDED.document, names = EXCLUDED.names;
        $$;
        """
    )
    op.execute(_REBUILD_ALL)
