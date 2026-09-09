"""coverage is nullable

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-04

ADR-032. `catalogs.coverage` was NOT NULL DEFAULT 'global', which contradicted ADR-020
(coverage is optional and describes true reach) and ADR-022 (`global` is a real tier meaning
worldwide). `data/recommended.json` made it concrete: none of the four seed catalogs declares
coverage, and storing all four as `global` is the registry asserting worldwide reach on their
behalf.

Existing `global` rows are left alone. They cannot be distinguished from genuinely worldwide
catalogs after the fact, and the only rows this migration can reach were written by a seed
that is being replaced in the same change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "catalogs",
        "coverage",
        existing_type=postgresql.ENUM(name="coverage_scope", create_type=False),
        nullable=True,
        server_default=None,
    )


def downgrade() -> None:
    op.execute(sa.text("UPDATE catalogs SET coverage = 'global' WHERE coverage IS NULL"))
    op.alter_column(
        "catalogs",
        "coverage",
        existing_type=postgresql.ENUM(name="coverage_scope", create_type=False),
        nullable=False,
        server_default="global",
    )
