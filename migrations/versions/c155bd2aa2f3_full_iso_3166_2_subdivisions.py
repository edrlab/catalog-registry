"""Every ISO 3166-2 subdivision, its names, and every type offered by default

Until now `subdivisions` held only the five codes some catalog used, so the back office could not
offer the regions of most countries (India had none). This loads the 5,043 subdivisions pycountry
26.2.16 knows for the countries in `countries` (all but the three French ones ADR-055 leaves out),
with their parents and lowercase type keys; their names from CLDR 48.2 in English and each
country's official languages (pycountry's English name where CLDR has none); and one
`country_subdivision_types` row per (country, type), so every country offers all of its ISO types
until an admin narrows them (Roni, 2026-11-06: "add all the things"). France keeps the seven types
ADR-055 chose until Hadrien's shorter list (Q15).

The rows are in `migrations/data/c155bd2aa2f3/` as CSV rather than literals: about 14,000 rows.
They were written by `scripts/generate_reference_data.py --full-subdivisions <dir>` (with pycountry
26.2.16, as `make reference-data` runs it) and are never regenerated in place; a later ISO or CLDR
update is a new migration carrying the difference.

`ON CONFLICT DO NOTHING` throughout, as in `b41f7c9ade52` and `2eb49a8f4664`: the five
subdivisions, their names and the twelve type rows already present are kept as they are. Ends with
`refresh_catalog_search`, as every reference-data migration does, since catalogs that use a
subdivision may gain names.

Downgrade removes what this revision added: subdivisions no catalog uses and that earlier
revisions did not add, their names, and the type rows beyond the original twelve.

Revision ID: c155bd2aa2f3
Revises: a3f19c7d8e42
Create Date: 2026-11-06

"""

import csv
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "c155bd2aa2f3"
down_revision: str | None = "a3f19c7d8e42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATA = Path(__file__).resolve().parents[1] / "data" / revision

#: Added by earlier revisions (`c8e1b73f2d04`, `b41f7c9ade52`); never removed here.
EARLIER_SUBDIVISIONS = ("BE-BRU", "BE-VLG", "BE-WAL", "CH-VS", "FR-IDF")

#: ADR-055 leaves three French types out ("no libraries expected"): overseas territory (FR-TF),
#: dependency (FR-CP) and European collectivity (FR-6AE). Their subdivisions are not loaded, so
#: every stored subdivision's type stays one its country offers.
LEFT_OUT = frozenset({"FR-TF", "FR-CP", "FR-6AE"})


def _read(name: str) -> list[dict[str, str]]:
    """The rows of *name*, without the left-out subdivisions. A subdivision whose parent is left out
    (the Alsace departments under FR-6AE) takes that parent's own parent instead."""
    with (DATA / name).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    parents = {row["code"]: row["parent_code"] for row in rows if "code" in row}
    kept = []
    for row in rows:
        if (row.get("code") or row.get("subdivision_code")) in LEFT_OUT:
            continue
        parent = row.get("parent_code")
        while parent in LEFT_OUT:
            parent = parents[parent]
        kept.append(row if parent == row.get("parent_code") else row | {"parent_code": parent})
    return kept


def upgrade() -> None:
    bind = op.get_bind()
    # Parents come first in the file, and one statement checks the foreign key at its end anyway.
    bind.execute(
        sa.text(
            "INSERT INTO subdivisions (code, country_alpha2, parent_code, subdivision_type) "
            "VALUES (:code, :country_alpha2, NULLIF(:parent_code, ''), :subdivision_type) "
            "ON CONFLICT (code) DO NOTHING"
        ),
        _read("subdivisions.csv"),
    )
    bind.execute(
        sa.text(
            "INSERT INTO subdivision_names (subdivision_code, language_tag, name) "
            "VALUES (:subdivision_code, :language_tag, :name) ON CONFLICT DO NOTHING"
        ),
        _read("subdivision_names.csv"),
    )
    # Every type a stored subdivision has is offered. France's seven (ADR-055) are already there.
    op.execute(
        "INSERT INTO country_subdivision_types (country_code, subdivision_type) "
        "SELECT DISTINCT country_alpha2, subdivision_type FROM subdivisions "
        "WHERE subdivision_type IS NOT NULL ON CONFLICT DO NOTHING"
    )
    op.execute("SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs")


def downgrade() -> None:
    bind = op.get_bind()
    added = [row["code"] for row in _read("subdivisions.csv")]
    removable = sa.text(
        "SELECT code FROM subdivisions WHERE code = ANY(CAST(:codes AS text[])) "
        "AND code <> ALL(CAST(:earlier AS text[])) "
        "AND NOT EXISTS (SELECT 1 FROM catalog_subdivisions c WHERE c.subdivision_code = code)"
    )
    codes = list(
        bind.execute(removable, {"codes": added, "earlier": list(EARLIER_SUBDIVISIONS)}).scalars()
    )
    bind.execute(
        sa.text("DELETE FROM subdivision_names WHERE subdivision_code = ANY(CAST(:c AS text[]))"),
        {"c": codes},
    )
    bind.execute(
        sa.text(
            "UPDATE subdivisions SET parent_code = NULL WHERE parent_code = ANY(CAST(:c AS text[]))"
        ),
        {"c": codes},
    )
    bind.execute(
        sa.text("DELETE FROM subdivisions WHERE code = ANY(CAST(:c AS text[]))"), {"c": codes}
    )
    # The twelve rows `2eb49a8f4664` added stay; the rest came from here.
    op.execute(
        """
        DELETE FROM country_subdivision_types t
         WHERE (t.country_code, t.subdivision_type) NOT IN (
               ('BE', 'region'), ('BE', 'province'), ('CH', 'canton'), ('CA', 'province'),
               ('CA', 'territory'), ('FR', 'metropolitan region'),
               ('FR', 'metropolitan department'),
               ('FR', 'metropolitan collectivity with special status'),
               ('FR', 'overseas departmental collectivity'), ('FR', 'overseas collectivity'),
               ('FR', 'overseas collectivity with special status'),
               ('FR', 'overseas unique territorial collectivity'))
        """
    )
    op.execute("SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs")
