-- Local and CI only. `gen_random_uuid()` is built into PostgreSQL 13+, so `catalogs.id` does not
-- need pgcrypto on either side; this keeps the extension present locally rather than relying on that.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- v0.2 search. The search migration (`847ef861dbdb`) runs `CREATE EXTENSION IF NOT EXISTS` for
-- both itself, so production gets them from `alembic upgrade head`, provided the migration user has
-- the `cloudsqlsuperuser` role. They are created here as well so a local database has them before
-- any migration runs.
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
