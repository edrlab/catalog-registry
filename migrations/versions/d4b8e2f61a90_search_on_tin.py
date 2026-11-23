"""Search on TIN: folded text per field, one BM25 index each, restriction flags on the row.

`catalog_search` drops its two halves (`document`, a weighted tsvector, and `names`, the trigram
text) for three folded text columns, one per weight: `title`, `city`, `places` (country and
subdivision names). Each has a `USING tin` index, PlanetScale's BM25 search index. The text is
stored through `public.fold()`: TIN's tokenizer folds `è` and `Ö` but keeps `ß`, `æ`, `ø` and `ł`,
and the query side folds with the same function.

The three restriction flags (has an OPDS 2.0 catalog link, is open, is audiobook-only) move onto
the row, so `restrict` is a column test instead of three `EXISTS` per hit. They are kept current by
the same rebuild, now also triggered by changes to links, kinds and publication types.

`registry_tinql()` turns the parsed chunks into a TINQL query, in the database so the typed text
is folded by the same `fold()` as the stored text.

`registry_query()`, the `registry_simple` analyzer and `pg_trgm` stay installed and unused; a later
migration drops them once search no longer needs a way back.

Needs the `tin` extension: on PlanetScale the default role may create it; locally and in CI the
image is `planetscale/lead`, TIN's open-source stand-in. The downgrade puts the tsvector and trigram
columns back and leaves the extension installed.

Revision ID: d4b8e2f61a90
Revises: eb44e431979f
Create Date: 2026-11-23
"""

# ruff: noqa: E501 - SQL blocks read better unwrapped

from collections.abc import Sequence

from alembic import op

revision: str = "d4b8e2f61a90"
down_revision: str | None = "eb44e431979f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REBUILD_ALL = "SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs"

#: Shared by both versions of the rebuild: lock, drop rows that are no longer searchable.
_LOCK_AND_PRUNE = """
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
"""

#: Country names in English and the country's official languages; the same for subdivisions.
_PLACE_NAMES = """
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
"""

_REFRESH_TIN = (
    """
        CREATE OR REPLACE FUNCTION public.refresh_catalog_search(p_ids uuid[]) RETURNS void
        LANGUAGE sql VOLATILE
        SET search_path = pg_catalog, public
        AS $$"""
    + _LOCK_AND_PRUNE
    + """
          INSERT INTO catalog_search (catalog_id, title, city, places, has_opds_catalog, is_open, audiobook_only)
          SELECT c.id,
                 public.fold(c.title),
                 public.fold(coalesce(c.city, '')),
                 public.fold(concat_ws(' ', cn.names, sn.names)),
                 -- The same rules as `domain/restrictions.py`; the tests run both on the same data.
                 EXISTS (SELECT 1 FROM links k
                          WHERE k.catalog_id = c.id AND k.rel = 'catalog'
                            AND lower(trim(split_part(k.media_type, ';', 1))) = 'application/opds+json'),
                 EXISTS (SELECT 1 FROM catalog_kinds x WHERE x.catalog_id = c.id AND x.kind = 'open'),
                 EXISTS (SELECT 1 FROM catalog_publication_types p
                          WHERE p.catalog_id = c.id AND p.publication_type = 'audiobook')
                 AND NOT EXISTS (SELECT 1 FROM catalog_publication_types p
                                  WHERE p.catalog_id = c.id AND p.publication_type <> 'audiobook')
            FROM catalogs c"""
    + _PLACE_NAMES
    + """
          ON CONFLICT (catalog_id) DO UPDATE
             SET title = EXCLUDED.title, city = EXCLUDED.city, places = EXCLUDED.places,
                 has_opds_catalog = EXCLUDED.has_opds_catalog, is_open = EXCLUDED.is_open,
                 audiobook_only = EXCLUDED.audiobook_only;
        $$;
        """
)

#: The rebuild as `a3f19c7d8e42` left it, for the downgrade.
_REFRESH_TSVECTOR = (
    """
        CREATE OR REPLACE FUNCTION public.refresh_catalog_search(p_ids uuid[]) RETURNS void
        LANGUAGE sql VOLATILE
        SET search_path = pg_catalog, public
        AS $$"""
    + _LOCK_AND_PRUNE
    + """
          INSERT INTO catalog_search (catalog_id, document, names)
          SELECT c.id,
                 setweight(to_tsvector('public.registry_simple', c.title), 'A')
              || setweight(to_tsvector('public.registry_simple', coalesce(c.city, '')), 'B')
              || setweight(to_tsvector('public.registry_simple', concat_ws(' ', cn.names, sn.names)), 'C'),
                 public.fold(concat_ws(' ', c.title, c.city, sn.names, cn.names))
            FROM catalogs c"""
    + _PLACE_NAMES
    + """
          ON CONFLICT (catalog_id) DO UPDATE
             SET document = EXCLUDED.document, names = EXCLUDED.names;
        $$;
        """
)

#: Tables whose rows change a catalog's restriction flags. Each gets the same trigger as
#: `catalog_subdivisions`, which rebuilds the catalog the row belongs to (both, on a move).
_FLAG_TABLES = ("links", "catalog_kinds", "catalog_publication_types")

_REGISTRY_TINQL = r"""
        -- Parsed chunks to TINQL, any chunk matching (OR). Each chunk is folded with `fold()` and
        -- split by TIN's own tokenizer, so a word here is a word in the index. A quoted chunk, or
        -- one the tokenizer splits (`ile-de-france`, `l'ecole` stays one), is an exact phrase. A
        -- single word matches exactly or within a typo allowance (`~1` from 4 letters, `~2` from 6;
        -- shorter words exact), and the last chunk of the query also as a prefix, so `lib` finds
        -- Library while it is still being typed. Without *typo_tolerant* (exclusions) words are
        -- exact. TIN scores a typo or prefix match only by the word as typed, so an exact match
        -- always ranks above one found only through a typo or a prefix.
        -- `fold()` lowercases, so no word can be an UPPER CASE TINQL keyword; the syntax
        -- characters are removed from each word and escaped inside phrases. NULL when nothing is left.
        CREATE FUNCTION public.registry_tinql(chunks text[], typo_tolerant boolean) RETURNS text
        LANGUAGE plpgsql STABLE SET search_path = pg_catalog, public AS $$
        DECLARE
          parts text[] := '{}';
          words text[]; word text; n int;
        BEGIN
          FOR n IN 1 .. coalesce(cardinality(chunks), 0) LOOP
            SELECT coalesce(array_agg(w ORDER BY ord), '{}') INTO words
              FROM tin.tokenize(public.fold(chunks[n])) WITH ORDINALITY AS t(tok, ord),
                   LATERAL (SELECT regexp_replace(tok, '[][()"~^*?\\:]', '', 'g') AS w) cleaned
             WHERE w <> '';
            CONTINUE WHEN cardinality(words) = 0;
            IF left(chunks[n], 1) = '"' OR cardinality(words) > 1 THEN
              parts := parts || ('"' || array_to_string(
                ARRAY(SELECT regexp_replace(w, '([_\\])', '\\\1', 'g') FROM unnest(words) w), ' ') || '"');
              CONTINUE;
            END IF;
            word := words[1];
            IF typo_tolerant AND char_length(word) >= 6 THEN
              word := word || '~2';
            ELSIF typo_tolerant AND char_length(word) >= 4 THEN
              word := word || '~1';
            END IF;
            IF typo_tolerant AND n = cardinality(chunks) THEN
              word := word || ' OR ' || words[1] || '*';
            END IF;
            parts := parts || word;
          END LOOP;
          RETURN nullif(array_to_string(parts, ' OR '), '');
        END $$;
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS tin")
    # Emptied and rebuilt at the end: the new columns are NOT NULL and have no sensible default.
    op.execute("TRUNCATE catalog_search")
    op.execute(
        """
        ALTER TABLE catalog_search
          DROP COLUMN document,
          DROP COLUMN names,
          ADD COLUMN title            text    NOT NULL,
          ADD COLUMN city             text    NOT NULL,
          ADD COLUMN places           text    NOT NULL,
          ADD COLUMN has_opds_catalog boolean NOT NULL,
          ADD COLUMN is_open          boolean NOT NULL,
          ADD COLUMN audiobook_only   boolean NOT NULL
        """
    )
    op.execute("CREATE INDEX ix_catalog_search_title_tin ON catalog_search USING tin (title)")
    op.execute("CREATE INDEX ix_catalog_search_city_tin ON catalog_search USING tin (city)")
    # b = 0: no BM25 length normalisation. The length of `places` is how many languages and
    # subdivisions a country has, which says nothing about how well a catalog matches.
    op.execute(
        "CREATE INDEX ix_catalog_search_places_tin ON catalog_search USING tin (places) "
        "WITH (b = 0)"
    )
    op.execute(_REFRESH_TIN)
    op.execute(
        "ALTER FUNCTION public.catalog_subdivisions_refresh_search() "
        "RENAME TO catalog_child_refresh_search"
    )
    for table in _FLAG_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_refresh_search
              AFTER INSERT OR UPDATE OR DELETE ON {table}
              FOR EACH ROW EXECUTE FUNCTION public.catalog_child_refresh_search();
            """
        )
    op.execute(_REGISTRY_TINQL)
    op.execute(_REBUILD_ALL)


def downgrade() -> None:
    op.execute("DROP FUNCTION public.registry_tinql(text[], boolean)")
    for table in _FLAG_TABLES:
        op.execute(f"DROP TRIGGER {table}_refresh_search ON {table}")
    op.execute(
        "ALTER FUNCTION public.catalog_child_refresh_search() "
        "RENAME TO catalog_subdivisions_refresh_search"
    )
    op.execute("TRUNCATE catalog_search")
    op.execute(
        """
        ALTER TABLE catalog_search
          DROP COLUMN title,
          DROP COLUMN city,
          DROP COLUMN places,
          DROP COLUMN has_opds_catalog,
          DROP COLUMN is_open,
          DROP COLUMN audiobook_only,
          ADD COLUMN document tsvector NOT NULL,
          ADD COLUMN names    text     NOT NULL
        """
    )
    op.execute("CREATE INDEX ix_catalog_search_document ON catalog_search USING gin (document)")
    op.execute(
        "CREATE INDEX ix_catalog_search_names_trgm ON catalog_search "
        "USING gin (names public.gin_trgm_ops)"
    )
    op.execute(_REFRESH_TSVECTOR)
    op.execute(_REBUILD_ALL)
