"""add FR-IDF and CH-VS subdivisions

`subdivisions` is not a full copy of ISO 3166-2. The initial migration seeds only the codes
some catalog actually references, which was `BE-BRU`, `BE-VLG` and `BE-WAL`. `data/libraries.json`
adds a Paris catalog covering Île-de-France and a Valais one covering the canton of Valais, and
`fk_catalog_subdivisions_subdivision_code_subdivisions` rejects a code that is not here — so the
seed fails before it writes a row.

Data only: no schema change, and `c8e1b73f2d04` is not edited (R6, forward-only). Every later
data set that names a new subdivision adds it the same way.

Revision ID: b41f7c9ade52
Revises: c8e1b73f2d04
Create Date: 2026-09-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b41f7c9ade52"
down_revision: str | None = "c8e1b73f2d04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (code, country_alpha2, parent_code, subdivision_type), the shape `c8e1b73f2d04` seeds.
#: Names are ISO 3166-2's own: FR-IDF is a région, CH-VS a canton.
SUBDIVISIONS = [
    ("CH-VS", "CH", None, "canton"),
    ("FR-IDF", "FR", None, "metropolitan region"),
]


def upgrade() -> None:
    op.bulk_insert(
        sa.table(
            "subdivisions",
            sa.column("code", sa.String(6)),
            sa.column("country_alpha2", sa.CHAR(2)),
            sa.column("parent_code", sa.String(6)),
            sa.column("subdivision_type", sa.String()),
        ),
        [
            {
                "code": code,
                "country_alpha2": country,
                "parent_code": parent,
                "subdivision_type": kind,
            }
            for code, country, parent, kind in SUBDIVISIONS
        ],
    )


def downgrade() -> None:
    # A catalog referencing one of these would block the delete, which is the correct
    # outcome: the reference has to go first.
    op.execute(
        sa.text("DELETE FROM subdivisions WHERE code IN :codes").bindparams(
            sa.bindparam("codes", [code for code, *_ in SUBDIVISIONS], expanding=True)
        )
    )
