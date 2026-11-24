"""Drop what the search before TIN needed: `registry_query()`, `registry_simple` and `pg_trgm`.

Nothing has used them since search moved to TIN (`d4b8e2f61a90`): the tsvector and trigram columns
are gone, and the query is built by `registry_tinql()`. `fold()` and `unaccent` stay; the stored
and the typed text both go through them.

The downgrade puts all three back exactly as `847ef861dbdb` made them, because the TIN migration's
own downgrade rebuilds the tsvector and trigram columns with them.

Revision ID: 06d8bef33737
Revises: d4b8e2f61a90
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "06d8bef33737"
down_revision: str | None = "d4b8e2f61a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP FUNCTION public.registry_query(text[], text[])")
    op.execute("DROP TEXT SEARCH CONFIGURATION public.registry_simple")
    op.execute("DROP EXTENSION pg_trgm")


def downgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    op.execute("CREATE TEXT SEARCH CONFIGURATION public.registry_simple (COPY = pg_catalog.simple)")
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
        CREATE FUNCTION public.registry_query(positives text[], negatives text[]) RETURNS tsquery
        LANGUAGE plpgsql STABLE SET search_path = pg_catalog, public
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
