-- Local and CI only. The migrations run `CREATE EXTENSION IF NOT EXISTS` for each of these
-- (`847ef861dbdb` unaccent, `d4b8e2f61a90` tin), so a deployed database gets them from
-- `alembic upgrade head`, provided the migration user may create them. They are created here as well
-- so a local database has them before any migration runs. Nothing else: a local database should not
-- have an extension the deployed one lacks.
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS tin;
