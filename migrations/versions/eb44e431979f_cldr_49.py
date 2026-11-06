"""CLDR 49: the names it adds

CLDR 49.0 (22 October 2026) changes little for the registry: seven country names and five
subdivision names (four stored: FR-TF is not loaded, ADR-055), mostly renamings ("U.S. Virgin
Islands", "Papua Pegunungan" with its spelling fixed, Nauru as "Naoero"). No official language
changes. Found by
`scripts/check_reference_sources.py`; the rows are `generate_reference_data.py --update` with
`CLDR_VERSION = "49.0.0"`, in `migrations/data/eb44e431979f/`.

Names CLDR 49 drops (four country names in local languages, five older subdivision forms) are kept
on purpose: a reader who types the old form still finds the place (ADR-053 amendment).

Downgrade deletes exactly these rows, by value.

Revision ID: eb44e431979f
Revises: 195c3d701630
Create Date: 2026-11-06

"""

import csv
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "eb44e431979f"
down_revision: str | None = "195c3d701630"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATA = Path(__file__).resolve().parents[1] / "data" / revision

#: (table, columns, the condition a row needs to be inserted).
TABLES = [
    ("country_languages", ("country_code", "language_tag"), "true"),
    ("country_names", ("country_code", "language_tag", "name"), "true"),
    (
        "subdivision_names",
        ("subdivision_code", "language_tag", "name"),
        "EXISTS (SELECT 1 FROM subdivisions WHERE code = CAST(:subdivision_code AS text))",
    ),
]


def _read(table: str) -> list[dict[str, str]]:
    with (DATA / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def upgrade() -> None:
    bind = op.get_bind()
    for table, columns, condition in TABLES:
        rows = _read(table)
        if rows:
            bind.execute(
                sa.text(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"SELECT {', '.join(f'CAST(:{column} AS text)' for column in columns)} "
                    f"WHERE {condition} "
                    "ON CONFLICT DO NOTHING"
                ),
                rows,
            )
    op.execute("SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs")


def downgrade() -> None:
    bind = op.get_bind()
    for table, columns, _condition in reversed(TABLES):
        rows = _read(table)
        if rows:
            bind.execute(
                sa.text(
                    f"DELETE FROM {table} WHERE "
                    + " AND ".join(f"{column} = :{column}" for column in columns)
                ),
                rows,
            )
    op.execute("SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs")
