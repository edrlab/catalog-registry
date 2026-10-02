"""v0.2 search schema

Extensions, `fold()`, the `registry_simple` analyzer, four reference tables, `catalog_search`, the
triggers that keep it current, and `registry_query()`. Blocks run in the order the reference
implementation tested them (`search-v0.2-final/04-reference-implementation.md` section 1), and
the SQL is not reworded. Decisions: ADR-051 to ADR-059.

Schema only. The reference rows arrive in the next revision, which also ends with the first
`refresh_catalog_search` over every catalog.

One deliberate difference from the reference text: the CHECK constraints are named (`ck_<table>_<name>`,
the repo's naming convention). `alembic check` compares check constraints by name, so Postgres's
generated names would make every future autogenerate propose dropping and re-adding them.

The downgrade drops everything this revision created, in reverse, and leaves the extensions
installed: another object in the database may depend on them, and dropping an extension is the
one step here that could take somebody else's data with it. It never touches v0 tables or rows.

Production: the migration user needs the `cloudsqlsuperuser` role to create the two extensions.
Cloud Run runs no migrations, so this is run by hand (see `ops-and-troubleshooting.md`).

Revision ID: 847ef861dbdb
Revises: b41f7c9ade52
Create Date: 2026-10-02

"""

# ruff: noqa: E501 - the SQL blocks are the tested reference text, kept verbatim

from collections.abc import Sequence

from alembic import op

revision: str = "847ef861dbdb"
down_revision: str | None = "b41f7c9ade52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        -- 1. Extensions. Production Cloud SQL has only plpgsql today; the migration user needs the
        --    cloudsqlsuperuser role to run these.
        CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;
        """
    )

    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS pg_trgm  WITH SCHEMA public;
        """
    )

    op.execute(
        """
        -- 2. fold(): the ONE normalisation used for trigram text, on both the stored side and the
        --    query side. unaccent(text) is only STABLE (it reads search_path to find its dictionary);
        --    naming the dictionary makes this safe to mark IMMUTABLE.
        CREATE FUNCTION public.fold(text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
        AS $$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, $1)) $$;
        """
    )

    op.execute(
        """
        -- 3. The analyzer: default parser, then unaccent, then simple (lowercase, keep).
        --    No stemmer and no stop-word list anywhere in the chain.
        CREATE TEXT SEARCH CONFIGURATION public.registry_simple (COPY = pg_catalog.simple);
        """
    )

    op.execute(
        """
        ALTER TEXT SEARCH CONFIGURATION public.registry_simple
          ALTER MAPPING FOR asciiword, asciihword, hword_asciipart,
                            word, hword, hword_part,
                            numword, numhword, hword_numpart
          WITH public.unaccent, pg_catalog.simple;
        """
    )

    op.execute(
        """
        -- 4. Reference tables. All loaded by data migrations from CLDR 48.2 / ISO 3166-2 snapshots.
        CREATE TABLE country_languages (
          country_code char(2)     NOT NULL REFERENCES countries (alpha2),
          language_tag varchar(35) NOT NULL CONSTRAINT ck_country_languages_language_tag_lowercase CHECK (language_tag = lower(language_tag)),
          PRIMARY KEY (country_code, language_tag)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE country_names (
          country_code char(2)     NOT NULL REFERENCES countries (alpha2),
          language_tag varchar(35) NOT NULL CONSTRAINT ck_country_names_language_tag_lowercase CHECK (language_tag = lower(language_tag)),
          name         text        NOT NULL CONSTRAINT ck_country_names_name_not_blank CHECK (length(trim(name)) > 0),
          PRIMARY KEY (country_code, language_tag, name)          -- several names per language (UK, United Kingdom)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE subdivision_names (
          subdivision_code varchar(6)  NOT NULL REFERENCES subdivisions (code),
          language_tag     varchar(35) NOT NULL CONSTRAINT ck_subdivision_names_language_tag_lowercase CHECK (language_tag = lower(language_tag)),
          name             text        NOT NULL CONSTRAINT ck_subdivision_names_name_not_blank CHECK (length(trim(name)) > 0),
          PRIMARY KEY (subdivision_code, language_tag, name)      -- Flandre and Région flamande
        );
        """
    )

    op.execute(
        """
        CREATE TABLE country_subdivision_types (                   -- back office picker, not read by search
          country_code     char(2) NOT NULL REFERENCES countries (alpha2),
          subdivision_type varchar NOT NULL CONSTRAINT ck_country_subdivision_types_subdivision_type_lowercase CHECK (subdivision_type = lower(subdivision_type)),
          PRIMARY KEY (country_code, subdivision_type)
        );
        """
    )

    op.execute(
        """
        -- Reverse lookup for "which catalogs use this subdivision" (name reloads, future filters).
        -- The PK (catalog_id, subdivision_code) cannot serve it.
        CREATE INDEX ix_catalog_subdivisions_subdivision_code ON catalog_subdivisions (subdivision_code);
        """
    )

    op.execute(
        """
        -- 5. The derived search table. One row per ACTIVE catalog, nothing else.
        CREATE TABLE catalog_search (
          catalog_id uuid     PRIMARY KEY REFERENCES catalogs (id) ON DELETE CASCADE,
          document   tsvector NOT NULL,   -- word half: A title, B country names, C subdivision names + city
          names      text     NOT NULL    -- trigram half: fold(title, city, subdivision names, country names)
        );
        """
    )

    op.execute(
        """
        CREATE INDEX ix_catalog_search_document ON catalog_search USING gin (document);
        """
    )

    op.execute(
        """
        CREATE INDEX ix_catalog_search_names_trgm ON catalog_search USING gin (names public.gin_trgm_ops);
        """
    )

    op.execute(
        """
        -- 6. Rebuild the rows of the given catalogs, set-based. One function serves the triggers
        --    (one id) and every bulk rebuild (all ids). Deletes rows of catalogs that are gone or
        --    not active. Place names: English + official languages of the PLACE's country.
        CREATE FUNCTION public.refresh_catalog_search(p_ids uuid[]) RETURNS void
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

    op.execute(
        """
        -- 7. Triggers: same transaction as the change, so search is never stale and no write path
        --    (seed, add, back office, a hand-written UPDATE) can forget it.
        CREATE FUNCTION public.catalogs_refresh_search() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
          PERFORM public.refresh_catalog_search(ARRAY[NEW.id]);
          RETURN NULL;
        END $$;
        """
    )

    op.execute(
        """
        CREATE TRIGGER catalogs_refresh_search
          AFTER INSERT OR UPDATE OF title, city, country_code, status ON catalogs
          FOR EACH ROW EXECUTE FUNCTION public.catalogs_refresh_search();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.catalog_subdivisions_refresh_search() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            PERFORM public.refresh_catalog_search(ARRAY[NEW.catalog_id]);
          ELSIF TG_OP = 'DELETE' THEN
            PERFORM public.refresh_catalog_search(ARRAY[OLD.catalog_id]);
          ELSE
            PERFORM public.refresh_catalog_search(ARRAY[OLD.catalog_id, NEW.catalog_id]);
          END IF;
          RETURN NULL;
        END $$;
        """
    )

    op.execute(
        """
        CREATE TRIGGER catalog_subdivisions_refresh_search
          AFTER INSERT OR UPDATE OR DELETE ON catalog_subdivisions
          FOR EACH ROW EXECUTE FUNCTION public.catalog_subdivisions_refresh_search();
        """
    )

    op.execute(
        """
        -- 8. Any-word tsquery from pre-split chunks: positives OR-ed, negatives AND NOT-ed.
        --    Chunks that produce no lexeme (punctuation only) are skipped, so no NOTICE and no error.
        CREATE FUNCTION public.registry_query(positives text[], negatives text[]) RETURNS tsquery
        LANGUAGE plpgsql STABLE SET search_path = pg_catalog, public
          -- websearch_to_tsquery sends a NOTICE for a chunk with no lexeme before the numnode()
          -- guard below can skip it. Harmless to the result, but noise on every punctuation query.
          SET client_min_messages = warning AS $$
        DECLARE q tsquery; part tsquery; chunk text;
        BEGIN
          FOREACH chunk IN ARRAY coalesce(positives, '{}') LOOP
            part := websearch_to_tsquery('public.registry_simple', chunk);
            CONTINUE WHEN numnode(part) = 0;
            q := CASE WHEN q IS NULL THEN part ELSE q || part END;
          END LOOP;
          IF q IS NULL THEN RETURN NULL; END IF;
          FOREACH chunk IN ARRAY coalesce(negatives, '{}') LOOP
            part := websearch_to_tsquery('public.registry_simple', chunk);
            CONTINUE WHEN numnode(part) = 0;
            q := q && !! part;
          END LOOP;
          RETURN q;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS catalog_subdivisions_refresh_search ON catalog_subdivisions;"
    )
    op.execute("DROP TRIGGER IF EXISTS catalogs_refresh_search ON catalogs;")
    op.execute("DROP FUNCTION IF EXISTS public.catalog_subdivisions_refresh_search();")
    op.execute("DROP FUNCTION IF EXISTS public.catalogs_refresh_search();")
    op.execute("DROP FUNCTION IF EXISTS public.registry_query(text[], text[]);")
    op.execute("DROP FUNCTION IF EXISTS public.refresh_catalog_search(uuid[]);")
    op.execute("DROP TABLE IF EXISTS catalog_search;")
    op.execute("DROP INDEX IF EXISTS ix_catalog_subdivisions_subdivision_code;")
    op.execute("DROP TABLE IF EXISTS country_subdivision_types;")
    op.execute("DROP TABLE IF EXISTS subdivision_names;")
    op.execute("DROP TABLE IF EXISTS country_names;")
    op.execute("DROP TABLE IF EXISTS country_languages;")
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS public.registry_simple;")
    op.execute("DROP FUNCTION IF EXISTS public.fold(text);")
