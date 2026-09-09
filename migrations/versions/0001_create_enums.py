"""create enums

Revision ID: 0001
Revises:
Create Date: 2026-09-01

All six native enum types, created before any table that references them. Native rather than
text-plus-check: these vocabularies come from a published JSON Schema, so they change rarely
and deliberately, and a migration is the correct amount of ceremony when they do.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "catalog_status": ("suggested", "active"),
    "catalog_kind": ("open", "public", "academic", "school", "specialized"),
    "catalog_color": ("gray", "red", "yellow", "blue", "green", "purple", "orange", "pink"),
    "publication_type": (
        "ebook",
        "audiobook",
        "comic",
        "newspaper",
        "magazine",
        "journal",
        "article",
    ),
    "coverage_scope": ("global", "country", "subdivisions", "local"),
    "link_rel": (
        "self",
        "catalog",
        "shelf",
        "icon",
        "authenticate",
        "alternate",
        "profile",
        "search",
    ),
}


def upgrade() -> None:
    for name, values in ENUMS.items():
        sa.Enum(*values, name=name).create(op.get_bind())


def downgrade() -> None:
    for name, values in reversed(ENUMS.items()):
        sa.Enum(*values, name=name).drop(op.get_bind())
