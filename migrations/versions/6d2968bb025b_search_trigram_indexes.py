"""Trigram indexes on the folded search text, for the matches TIN cannot make.

TIN forgives a typo only after the first letter, and a swap of two letters costs it two edits, so
`xutenberg` and `pairs` find nothing. `pg_trgm`'s word similarity compares three-letter pieces and
has no such blind spots. Search adds those catalogs after TIN's own results. The text is the folded
`title`, `city` and `places` `catalog_search` already stores, so no new data is kept.

Revision ID: 6d2968bb025b
Revises: 8805df0e33ba
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "6d2968bb025b"
down_revision: str | None = "8805df0e33ba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("title", "city", "places")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    for column in _COLUMNS:
        op.execute(
            f"CREATE INDEX ix_catalog_search_{column}_trgm ON catalog_search "
            f"USING gin ({column} public.gin_trgm_ops)"
        )


def downgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"DROP INDEX ix_catalog_search_{column}_trgm")
