"""create reference tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "countries",
        sa.Column("alpha2", sa.CHAR(length=2), nullable=False),
        sa.Column("alpha3", sa.CHAR(length=3), nullable=False),
        sa.Column("numeric3", sa.CHAR(length=3), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.PrimaryKeyConstraint("alpha2", name=op.f("pk_countries")),
        sa.UniqueConstraint("alpha3", name=op.f("uq_countries_alpha3")),
    )
    op.create_table(
        "subdivisions",
        sa.Column("code", sa.String(length=6), nullable=False),
        sa.Column("country_alpha2", sa.CHAR(length=2), nullable=False),
        sa.Column("parent_code", sa.String(length=6), nullable=True),
        sa.Column("subdivision_type", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_subdivisions")),
        sa.ForeignKeyConstraint(
            ["country_alpha2"],
            ["countries.alpha2"],
            name=op.f("fk_subdivisions_country_alpha2_countries"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_code"],
            ["subdivisions.code"],
            name=op.f("fk_subdivisions_parent_code_subdivisions"),
        ),
    )


def downgrade() -> None:
    op.drop_table("subdivisions")
    op.drop_table("countries")
