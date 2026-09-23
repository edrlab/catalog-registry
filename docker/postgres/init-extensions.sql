-- Local and CI only. Checked against the Cloud SQL instance on 2026-09-24: production has
-- `plpgsql` and nothing else, so neither line here describes it.
--
-- `gen_random_uuid()` is built into PostgreSQL 13+, so `catalogs.id` does not need pgcrypto
-- on either side; this keeps the extension present locally rather than relying on that.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- v0.2 search. Created here so the search work has it locally from the start. It is NOT in
-- production, and nothing creates it there: whoever ships search runs `CREATE EXTENSION
-- pg_trgm` against Cloud SQL as part of that change, or it fails on the first query.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
