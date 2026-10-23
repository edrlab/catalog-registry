"""Catalog lifecycle statuses, a version column, and the actors table.

`catalog_status` was `suggested` / `active`. It becomes `draft` / `published` / `unpublished` /
`archived`:

| before      | after       | why                                                          |
|-------------|-------------|--------------------------------------------------------------|
| `active`    | `published` | exactly the catalogs the public API served, still served      |
| `suggested` | `draft`     | not visible to the public, as before                          |

Nothing becomes visible or invisible to a reader. The enum is replaced rather than extended
because PostgreSQL cannot drop a value from an enum, so the downgrade could not otherwise remove
the new ones.

The downgrade maps `published` back to `active` and `draft` to `suggested`. `unpublished` and
`archived` have no old equivalent. They become `suggested`, the only old value that is hidden from
the public lists, so a downgrade never makes a catalog more visible than it was. The direct-link
visibility of `unpublished` is lost, and `published_at` is kept.

Also here:

* `catalogs.version`, the integer the admin API returns so an editor can detect that a catalog
  changed under them. Nothing increments it yet; the write endpoints do.
* `actors`, the people allowed into the admin API, keyed by lowercased email. No rows are seeded
  here (the emails are not in the repository); the first admin is added with
  `python -m registry.cli actor add`.
* `refresh_catalog_search` indexes `published` rather than `active`. The rows in
  `catalog_search` are unchanged, because the same catalogs qualify.

Revision ID: a3f19c7d8e42
Revises: 6d2a91c5e7b3
Create Date: 2026-10-23
"""

# ruff: noqa: E501 - the SQL blocks are the tested reference text, kept verbatim

from collections.abc import Sequence

from alembic import op

revision: str = "a3f19c7d8e42"
down_revision: str | None = "6d2a91c5e7b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refresh_catalog_search(searchable: str) -> str:
    """The body of `6d2a91c5e7b3`, with the status that gets indexed as the only difference."""
    return f"""
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
             AND NOT EXISTS (SELECT 1 FROM catalogs c WHERE c.id = cs.catalog_id AND c.status = '{searchable}');

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
           WHERE c.id = ANY (p_ids) AND c.status = '{searchable}'
          ON CONFLICT (catalog_id) DO UPDATE
             SET document = EXCLUDED.document, names = EXCLUDED.names;
        $$;
        """


#: A column cannot change type while a trigger watches it (`UPDATE OF status`), an index predicate
#: mentions it, or a check constraint reads it, so all three are dropped first and put back after.
_DROP_STATUS_DEPENDENTS = (
    "DROP TRIGGER catalogs_refresh_search ON catalogs",
    "DROP INDEX ix_catalogs_recommended",
    "ALTER TABLE catalogs DROP CONSTRAINT ck_catalogs_published_when_active",
    "ALTER TABLE catalogs ALTER COLUMN status DROP DEFAULT",
    "ALTER TYPE catalog_status RENAME TO catalog_status_old",
)

_RESTORE_TRIGGER = """
CREATE TRIGGER catalogs_refresh_search
  AFTER INSERT OR UPDATE OF title, city, country_code, status ON catalogs
  FOR EACH ROW EXECUTE FUNCTION public.catalogs_refresh_search();
"""


def upgrade() -> None:
    for statement in _DROP_STATUS_DEPENDENTS:
        op.execute(statement)
    op.execute(
        "CREATE TYPE catalog_status AS ENUM ('draft', 'published', 'unpublished', 'archived')"
    )
    op.execute(
        """
        ALTER TABLE catalogs ALTER COLUMN status TYPE catalog_status USING (
          CASE status::text WHEN 'active' THEN 'published' ELSE 'draft' END
        )::catalog_status
        """
    )
    op.execute("ALTER TABLE catalogs ALTER COLUMN status SET DEFAULT 'draft'")
    op.execute("DROP TYPE catalog_status_old")
    # Only `published` needs a date. An unpublished or archived catalog keeps the one it had.
    op.execute(
        """
        ALTER TABLE catalogs ADD CONSTRAINT ck_catalogs_published_when_published
          CHECK (status <> 'published' OR published_at IS NOT NULL)
        """
    )
    op.execute(
        "CREATE INDEX ix_catalogs_recommended ON catalogs (recommended) "
        "WHERE recommended AND status = 'published'"
    )
    op.execute(_refresh_catalog_search("published"))
    op.execute(_RESTORE_TRIGGER)

    op.execute("ALTER TABLE catalogs ADD COLUMN version integer NOT NULL DEFAULT 1")
    op.execute(
        "ALTER TABLE catalogs ADD CONSTRAINT ck_catalogs_version_positive CHECK (version >= 1)"
    )
    # Admin list filters: status is on every request, and the list is ordered by title.
    op.execute("CREATE INDEX ix_catalogs_status ON catalogs (status)")

    op.execute("CREATE TYPE actor_role AS ENUM ('viewer', 'editor', 'admin')")
    op.execute(
        """
        CREATE TABLE actors (
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          email text NOT NULL,
          name text,
          role actor_role NOT NULL DEFAULT 'viewer',
          is_active boolean NOT NULL DEFAULT true,
          identity_issuer text,
          identity_subject text,
          created_at timestamptz NOT NULL DEFAULT now(),
          last_seen_at timestamptz,
          CONSTRAINT pk_actors PRIMARY KEY (id),
          CONSTRAINT uq_actors_email UNIQUE (email),
          CONSTRAINT ck_actors_email_lowercase CHECK (email = lower(email)),
          CONSTRAINT ck_actors_email_not_blank CHECK (length(trim(email)) > 0)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE actors")
    op.execute("DROP TYPE actor_role")

    op.execute("DROP INDEX ix_catalogs_status")
    op.execute("ALTER TABLE catalogs DROP CONSTRAINT ck_catalogs_version_positive")
    op.execute("ALTER TABLE catalogs DROP COLUMN version")

    op.execute("DROP TRIGGER catalogs_refresh_search ON catalogs")
    op.execute("DROP INDEX ix_catalogs_recommended")
    op.execute("ALTER TABLE catalogs DROP CONSTRAINT ck_catalogs_published_when_published")
    op.execute("ALTER TABLE catalogs ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE catalog_status RENAME TO catalog_status_new")
    op.execute("CREATE TYPE catalog_status AS ENUM ('suggested', 'active')")
    op.execute(
        """
        ALTER TABLE catalogs ALTER COLUMN status TYPE catalog_status USING (
          CASE status::text WHEN 'published' THEN 'active' ELSE 'suggested' END
        )::catalog_status
        """
    )
    op.execute("ALTER TABLE catalogs ALTER COLUMN status SET DEFAULT 'suggested'")
    op.execute("DROP TYPE catalog_status_new")
    op.execute(
        """
        ALTER TABLE catalogs ADD CONSTRAINT ck_catalogs_published_when_active
          CHECK (status <> 'active' OR published_at IS NOT NULL)
        """
    )
    op.execute(
        "CREATE INDEX ix_catalogs_recommended ON catalogs (recommended) "
        "WHERE recommended AND status = 'active'"
    )
    op.execute(_refresh_catalog_search("active"))
    op.execute(_RESTORE_TRIGGER)
    # Catalogs that were unpublished or archived are now `suggested`: drop their search rows.
    op.execute("SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs")
