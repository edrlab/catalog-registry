"""create updated_at trigger

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-01

A trigger rather than an ORM ``onupdate``: it also fires for the direct SQL the seed script
and any future data migration perform. Autogenerate does not produce triggers, so this is
hand-written by definition.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN NEW.updated_at = now(); RETURN NEW; END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER catalogs_set_updated_at
        BEFORE UPDATE ON catalogs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS catalogs_set_updated_at ON catalogs;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
