"""create catalog tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str) -> postgresql.ENUM:
    """Reference the type created in 0001 without trying to create it again."""
    return postgresql.ENUM(name=name, create_type=False)


def _child_table(name: str, value_column: sa.Column, *extra: sa.schema.SchemaItem) -> None:
    """The four value collections share a shape: composite PK, cascade, index on catalog_id."""
    op.create_table(
        name,
        sa.Column("catalog_id", postgresql.UUID(as_uuid=True), nullable=False),
        value_column,
        sa.PrimaryKeyConstraint("catalog_id", value_column.name, name=op.f(f"pk_{name}")),
        sa.ForeignKeyConstraint(
            ["catalog_id"],
            ["catalogs.id"],
            ondelete="CASCADE",
            name=op.f(f"fk_{name}_catalog_id_catalogs"),
        ),
        *extra,
    )
    op.create_index(f"ix_{name}_catalog_id", name, ["catalog_id"])


def upgrade() -> None:
    op.create_table(
        "catalogs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("status", _enum("catalog_status"), server_default="suggested", nullable=False),
        sa.Column("recommended", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", _enum("catalog_color"), server_default="gray", nullable=False),
        sa.Column("country_code", sa.CHAR(length=2), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("coverage", _enum("coverage_scope"), server_default="global", nullable=False),
        sa.Column("submitter_name", sa.Text(), nullable=True),
        sa.Column("submitter_email", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalogs")),
        sa.ForeignKeyConstraint(
            ["country_code"], ["countries.alpha2"], name=op.f("fk_catalogs_country_code_countries")
        ),
        sa.CheckConstraint("length(trim(title)) > 0", name=op.f("ck_catalogs_title_not_blank")),
        sa.CheckConstraint(
            "country_code = upper(country_code)", name=op.f("ck_catalogs_country_uppercase")
        ),
        sa.CheckConstraint(
            "status <> 'active' OR published_at IS NOT NULL",
            name=op.f("ck_catalogs_published_when_active"),
        ),
    )
    # Partial: the top-level feed's only query. Right shape over ten rows, still right at
    # ten thousand.
    op.create_index(
        "ix_catalogs_recommended",
        "catalogs",
        ["recommended"],
        postgresql_where=sa.text("recommended AND status = 'active'"),
    )
    op.create_index("ix_catalogs_country_code", "catalogs", ["country_code"])

    _child_table("catalog_kinds", sa.Column("kind", _enum("catalog_kind"), nullable=False))
    _child_table(
        "catalog_publication_types",
        sa.Column("publication_type", _enum("publication_type"), nullable=False),
    )
    _child_table(
        "catalog_languages",
        sa.Column("language_tag", sa.String(length=35), nullable=False),
        sa.CheckConstraint(
            "language_tag = lower(language_tag)",
            name=op.f("ck_catalog_languages_language_tag_lowercase"),
        ),
    )
    _child_table(
        "catalog_subdivisions",
        sa.Column("subdivision_code", sa.String(length=6), nullable=False),
        sa.CheckConstraint(
            "subdivision_code = upper(subdivision_code)",
            name=op.f("ck_catalog_subdivisions_subdivision_code_uppercase"),
        ),
        sa.ForeignKeyConstraint(
            ["subdivision_code"],
            ["subdivisions.code"],
            name=op.f("fk_catalog_subdivisions_subdivision_code_subdivisions"),
        ),
    )


def downgrade() -> None:
    for name in (
        "catalog_subdivisions",
        "catalog_languages",
        "catalog_publication_types",
        "catalog_kinds",
    ):
        op.drop_table(name)
    op.drop_table("catalogs")
