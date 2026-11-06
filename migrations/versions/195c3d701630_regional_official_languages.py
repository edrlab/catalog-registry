"""Every language official anywhere in a country, and the names in them

ADR-053 counted only CLDR's country-wide `official` status, so India had 2 languages of its 22
and South Africa 1 of 11; 41 countries were missing 146 languages that CLDR marks official in a
region (`official_regional`) or de facto. Roni widened the rule on 2026-11-06: every CLDR
official status counts, plus the few official languages CLDR misses, each with its source
(`HAND_COUNTRY_LANGUAGES` in `scripts/generate_reference_data.py`: India's Bodo, Dogri and
Manipuri, from the Eighth Schedule of the Constitution).

This adds only what no earlier migration holds: the new `country_languages` rows, country names
in the new languages, and subdivision names in them (CLDR 48.2 still; a CLDR bump is its own
migration). The rows are the CSV files in `migrations/data/195c3d701630/`, written by
`generate_reference_data.py --update`. A subdivision name is inserted only when its subdivision
is stored (`c155bd2aa2f3` leaves three French ones out). Ends with `refresh_catalog_search`, so
catalogs are found under the new names.

Downgrade deletes exactly these rows, by value.

Revision ID: 195c3d701630
Revises: c155bd2aa2f3
Create Date: 2026-11-06

"""

import csv
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "195c3d701630"
down_revision: str | None = "c155bd2aa2f3"
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
