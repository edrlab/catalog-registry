"""Triggers keep `catalog_search` current, in the same transaction (ADR-056, plan section 3).

Every write here is plain SQL or a re-import, never a call to `refresh_catalog_search`: the
point is that no write path can forget the index. The session never commits to the database
(the fixture's outer transaction is rolled back), so "visible straight after the statement"
is also "visible in the same transaction".
"""

import copy
import json
import re
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import import_catalog_document
from tests.conftest import LIBRARIES_FILE
from tests.search_helpers import ALL_TITLES, catalog_search_row, search_rows

pytestmark = pytest.mark.integration

PARIS = "Bibliothèque numérique de Paris"
LIRTUEL = "Lirtuel"
VALAIS = "Médiathèque Valais"
LIBRIVOX = "Librivox"


async def found(session: AsyncSession, query: str) -> list[str]:
    return [title for title, _, _ in await search_rows(session, query)]


async def sql(session: AsyncSession, statement: str, **params: Any) -> None:
    await session.execute(text(statement), params)


async def catalog_id(session: AsyncSession, title: str) -> uuid.UUID:
    value = await session.scalar(text("SELECT id FROM catalogs WHERE title = :t"), {"t": title})
    assert value is not None
    return value  # type: ignore[no-any-return]


async def search_row_count(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT count(*) FROM catalog_search")) or 0)


def has_label(document: str, word: str, label: str) -> bool:
    """Is *word* in the tsvector text with a position carrying *label*, e.g. `'paris':4A`?"""
    match = re.search(rf"'{re.escape(word)}':([0-9A-D,]+)", document)
    return bool(match) and any(part.endswith(label) for part in match.group(1).split(","))


async def test_every_seeded_catalog_has_exactly_one_row(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    assert await search_row_count(db_session) == len(ALL_TITLES)
    for title in ALL_TITLES:
        assert await catalog_search_row(db_session, title) is not None, title


async def test_subdivision_names_written_after_the_catalog_row_are_in_the_document(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """The import writes the catalog, then its subdivisions; the second write must refresh."""
    row = await catalog_search_row(db_session, LIRTUEL)

    assert row is not None
    for word in ("wallonie", "bruxelles", "brussel"):
        assert has_label(row["document"], word, "C"), word
    assert has_label(row["document"], "lirtuel", "A")
    assert has_label(row["document"], "belgique", "B")


async def test_renaming_a_catalog_indexes_the_new_title_in_the_same_transaction(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await sql(
        db_session, "UPDATE catalogs SET title = 'Zanzibar archive' WHERE title = :t", t=LIBRIVOX
    )

    assert await found(db_session, "zanzibar") == ["Zanzibar archive"]
    assert LIBRIVOX not in await found(db_session, "librivox")
    row = await catalog_search_row(db_session, "Zanzibar archive")
    assert row is not None
    assert has_label(row["document"], "zanzibar", "A")
    assert "zanzibar" in row["names"]
    assert "librivox" not in row["names"]


async def test_a_city_set_by_a_plain_update_is_indexed_at_label_c_and_removing_it_unindexes_it(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await sql(db_session, "UPDATE catalogs SET city = 'Namur' WHERE title = :t", t=LIRTUEL)

    assert await found(db_session, "namur") == [LIRTUEL]
    row = await catalog_search_row(db_session, LIRTUEL)
    assert row is not None
    assert has_label(row["document"], "namur", "C")
    assert "namur" in row["names"]

    await sql(db_session, "UPDATE catalogs SET city = NULL WHERE title = :t", t=LIRTUEL)

    assert await found(db_session, "namur") == []


async def test_a_country_change_replaces_the_country_names(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await sql(db_session, "UPDATE catalogs SET country_code = 'FR' WHERE title = :t", t=VALAIS)

    assert await found(db_session, "schweiz") == []
    assert VALAIS in await found(db_session, "france")
    row = await catalog_search_row(db_session, VALAIS)
    assert row is not None
    assert has_label(row["document"], "france", "B")
    assert not has_label(row["document"], "suisse", "B")


async def test_removing_the_country_keeps_the_row_and_drops_its_names(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await sql(db_session, "UPDATE catalogs SET country_code = NULL WHERE title = :t", t=VALAIS)

    row = await catalog_search_row(db_session, VALAIS)
    assert row is not None
    assert "suisse" not in row["names"]
    assert await found(db_session, "mediatheque") == [VALAIS]


async def test_adding_a_subdivision_indexes_its_names(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    valais = await catalog_id(db_session, VALAIS)
    await sql(
        db_session,
        "INSERT INTO catalog_subdivisions (catalog_id, subdivision_code) VALUES (:c, 'FR-IDF')",
        c=valais,
    )

    assert set(await found(db_session, "ile-de-france")) == {PARIS, VALAIS}


async def test_removing_a_subdivision_removes_its_names_from_the_document(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    assert LIRTUEL in await found(db_session, "wallonie")
    lirtuel = await catalog_id(db_session, LIRTUEL)

    await sql(
        db_session,
        "DELETE FROM catalog_subdivisions WHERE catalog_id = :c AND subdivision_code = 'BE-WAL'",
        c=lirtuel,
    )

    assert await found(db_session, "wallonie") == []
    row = await catalog_search_row(db_session, LIRTUEL)
    assert row is not None
    assert "wallonie" not in row["names"]
    assert has_label(row["document"], "bruxelles", "C")  # BE-BRU is still there


async def test_changing_a_subdivision_code_swaps_the_names(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    lirtuel = await catalog_id(db_session, LIRTUEL)

    await sql(
        db_session,
        "UPDATE catalog_subdivisions SET subdivision_code = 'CH-VS' "
        "WHERE catalog_id = :c AND subdivision_code = 'BE-WAL'",
        c=lirtuel,
    )

    assert await found(db_session, "wallonie") == []
    assert set(await found(db_session, "valais")) == {LIRTUEL, VALAIS}


async def test_moving_a_subdivision_row_to_another_catalog_refreshes_both(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """An UPDATE that changes `catalog_id` must refresh the old catalog and the new one."""
    lirtuel = await catalog_id(db_session, LIRTUEL)
    valais = await catalog_id(db_session, VALAIS)
    await sql(
        db_session,
        "INSERT INTO catalog_subdivisions (catalog_id, subdivision_code) VALUES (:c, 'FR-IDF')",
        c=lirtuel,
    )
    assert LIRTUEL in await found(db_session, "ile-de-france")

    await sql(
        db_session,
        "UPDATE catalog_subdivisions SET catalog_id = :v WHERE catalog_id = :l "
        "AND subdivision_code = 'FR-IDF'",
        v=valais,
        l=lirtuel,
    )

    assert set(await found(db_session, "ile-de-france")) == {PARIS, VALAIS}


async def test_deactivating_a_catalog_deletes_its_row_and_reactivating_restores_it(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    before = await catalog_search_row(db_session, VALAIS)

    await sql(db_session, "UPDATE catalogs SET status = 'suggested' WHERE title = :t", t=VALAIS)

    assert await catalog_search_row(db_session, VALAIS) is None
    assert await found(db_session, "valais") == []
    assert await search_row_count(db_session) == len(ALL_TITLES) - 1

    await sql(db_session, "UPDATE catalogs SET status = 'active' WHERE title = :t", t=VALAIS)

    assert await catalog_search_row(db_session, VALAIS) == before
    assert await found(db_session, "valais") == [VALAIS]


async def test_deleting_a_catalog_removes_its_row_through_the_cascade(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await sql(db_session, "DELETE FROM catalogs WHERE title = :t", t=VALAIS)

    assert await found(db_session, "valais") == []
    assert await search_row_count(db_session) == len(ALL_TITLES) - 1
    orphans = await db_session.scalar(
        text(
            "SELECT count(*) FROM catalog_search WHERE catalog_id NOT IN (SELECT id FROM catalogs)"
        )
    )
    assert orphans == 0


async def test_a_catalog_inserted_with_plain_sql_is_searchable_with_no_application_code(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await sql(
        db_session,
        "INSERT INTO catalogs (id, title, status, country_code, city, published_at) "
        "VALUES (gen_random_uuid(), 'Handwritten insert', 'active', 'CH', 'Sion', now())",
    )

    assert await found(db_session, "handwritten") == ["Handwritten insert"]
    assert "Handwritten insert" in await found(db_session, "schweiz")
    assert "Handwritten insert" in await found(db_session, "sion")


async def test_a_catalog_inserted_as_suggested_gets_no_row(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await sql(
        db_session,
        "INSERT INTO catalogs (id, title, status) "
        "VALUES (gen_random_uuid(), 'Draft only', 'suggested')",
    )

    assert await found(db_session, "draft") == []
    assert await search_row_count(db_session) == len(ALL_TITLES)


async def test_a_rolled_back_change_leaves_the_index_as_it_was(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """Same transaction: the index change is undone with the change that caused it."""
    before = await catalog_search_row(db_session, LIBRIVOX)

    nested = await db_session.begin_nested()
    await sql(db_session, "UPDATE catalogs SET title = 'Zanzibar' WHERE title = :t", t=LIBRIVOX)
    assert await found(db_session, "zanzibar") == ["Zanzibar"]
    await nested.rollback()

    assert await found(db_session, "zanzibar") == []
    assert await catalog_search_row(db_session, LIBRIVOX) == before


async def test_a_new_subdivision_name_is_searchable_only_after_the_affected_catalogs_refresh(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """Reference data has no trigger (ADR-056): a data migration ends with a refresh."""
    await sql(
        db_session,
        "INSERT INTO subdivision_names (subdivision_code, language_tag, name) "
        "VALUES ('BE-WAL', 'fr', 'Wallonie picarde')",
    )
    assert await found(db_session, "picarde") == []

    await sql(
        db_session,
        "SELECT public.refresh_catalog_search(array_agg(catalog_id)) "
        "FROM catalog_subdivisions WHERE subdivision_code = 'BE-WAL'",
    )

    assert await found(db_session, "picarde") == [LIRTUEL]


async def test_a_full_rebuild_equals_the_rows_the_triggers_built(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    query = "SELECT catalog_id, document::text AS document, names FROM catalog_search"
    built = {r.catalog_id: (r.document, r.names) for r in await db_session.execute(text(query))}
    assert len(built) == len(ALL_TITLES)

    # Empty the table first, otherwise the rebuild's upsert would trivially "equal" itself.
    await sql(db_session, "DELETE FROM catalog_search")
    assert await search_row_count(db_session) == 0
    await sql(db_session, "SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs")

    rebuilt = {r.catalog_id: (r.document, r.names) for r in await db_session.execute(text(query))}
    assert rebuilt == built


async def test_a_rebuild_removes_rows_of_catalogs_that_are_not_active(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """The function also deletes: a stray row for an inactive catalog does not survive it."""
    valais = await catalog_id(db_session, VALAIS)
    await sql(db_session, "UPDATE catalogs SET status = 'suggested' WHERE id = :c", c=valais)
    await sql(
        db_session,
        "INSERT INTO catalog_search (catalog_id, document, names) "
        "VALUES (:c, to_tsvector('simple', 'stray'), 'stray')",
        c=valais,
    )
    assert await search_row_count(db_session) == len(ALL_TITLES)

    await sql(db_session, "SELECT public.refresh_catalog_search(ARRAY[CAST(:c AS uuid)])", c=valais)

    assert await catalog_search_row(db_session, VALAIS) is None


def lirtuel_document() -> dict[str, Any]:
    feed = json.loads(LIBRARIES_FILE.read_text(encoding="utf-8"))
    return copy.deepcopy(next(c for c in feed["catalogs"] if c["metadata"]["title"] == LIRTUEL))


async def test_reseeding_with_a_changed_city_updates_the_row(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    document = lirtuel_document()

    document["metadata"]["city"] = "Namur"
    _, created = await import_catalog_document(db_session, document, recommended=False)
    await db_session.flush()
    assert not created
    assert await found(db_session, "namur") == [LIRTUEL]

    document["metadata"]["city"] = "Liège"
    await import_catalog_document(db_session, document, recommended=False)
    await db_session.flush()

    assert await found(db_session, "liege") == [LIRTUEL]
    assert await found(db_session, "namur") == []
    assert await search_row_count(db_session) == len(ALL_TITLES)


async def test_the_paris_city_is_indexed_at_label_c_and_dropping_it_removes_that_label(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    row = await catalog_search_row(db_session, PARIS)
    assert row is not None
    assert has_label(row["document"], "paris", "A")
    assert has_label(row["document"], "paris", "C")

    feed = json.loads(LIBRARIES_FILE.read_text(encoding="utf-8"))
    document = next(c for c in feed["catalogs"] if c["metadata"]["title"] == PARIS)
    del document["metadata"]["city"]
    await import_catalog_document(db_session, document, recommended=False)
    await db_session.flush()

    row = await catalog_search_row(db_session, PARIS)
    assert row is not None
    assert has_label(row["document"], "paris", "A")
    assert not has_label(row["document"], "paris", "C")


async def test_reseeding_a_changed_subdivision_list_updates_the_row(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    document = lirtuel_document()
    document["metadata"]["subdivisions"] = ["BE-BRU"]

    await import_catalog_document(db_session, document, recommended=False)
    await db_session.flush()

    assert await found(db_session, "wallonie") == []
    assert LIRTUEL in await found(db_session, "bruxelles")
