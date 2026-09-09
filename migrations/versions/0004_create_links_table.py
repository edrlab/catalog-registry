"""create links table

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("catalog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("href", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("rel", postgresql.ENUM(name="link_rel", create_type=False), nullable=False),
        sa.Column("templated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("authentication", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_links")),
        sa.ForeignKeyConstraint(
            ["catalog_id"],
            ["catalogs.id"],
            ondelete="CASCADE",
            name=op.f("fk_links_catalog_id_catalogs"),
        ),
        # Satisfies the schema's `uniqueItems` on the links array.
        sa.UniqueConstraint("catalog_id", "rel", "href", name=op.f("uq_links_catalog_rel_href")),
        sa.CheckConstraint(
            "NOT templated OR rel = 'search'", name=op.f("ck_links_templated_only_search")
        ),
    )
    op.create_index("ix_links_catalog_id", "links", ["catalog_id"])


def downgrade() -> None:
    op.drop_table("links")
