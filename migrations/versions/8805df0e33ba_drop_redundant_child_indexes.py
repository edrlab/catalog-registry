"""Drop the `catalog_id` index of the four child tables: each primary key already starts with it.

`catalog_kinds (catalog_id, kind)`, `catalog_languages (catalog_id, language_tag)`,
`catalog_publication_types (catalog_id, publication_type)` and
`catalog_subdivisions (catalog_id, subdivision_code)` serve every lookup by catalog through their
primary key. The extra indexes were never used and cost a write on every save (a save replaces a
catalog's child rows). `ix_catalog_subdivisions_subdivision_code` stays: it is not redundant.

Revision ID: 8805df0e33ba
Revises: e16d69989d0d
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8805df0e33ba"
down_revision: str | None = "e16d69989d0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "catalog_kinds",
    "catalog_languages",
    "catalog_publication_types",
    "catalog_subdivisions",
)


def upgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_catalog_id", table_name=table)


def downgrade() -> None:
    for table in _TABLES:
        op.create_index(f"ix_{table}_catalog_id", table, ["catalog_id"])
