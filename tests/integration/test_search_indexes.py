"""The TIN indexes, the query `registry_tinql` builds, and the NOTICE trap.

Locally and in CI the indexes are Lead's, which return every page as a candidate, so the plan says
only that an index *can* be used; whether it is fast is measured on PlanetScale, not here.
"""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from tests.search_helpers import search_rows

pytestmark = pytest.mark.integration


async def test_each_searched_field_has_its_own_tin_index(db_session: AsyncSession) -> None:
    """One index per field: TIN indexes one column, and one column per weight is what lets a
    title match outrank a city match."""
    rows = await db_session.execute(
        text(
            "SELECT i.relname, a.amname FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_am a ON a.oid = i.relam "
            "WHERE x.indrelid = 'catalog_search'::regclass AND NOT x.indisprimary ORDER BY 1"
        )
    )

    assert [tuple(r) for r in rows] == [
        ("ix_catalog_search_city_tin", "tin"),
        ("ix_catalog_search_places_tin", "tin"),
        ("ix_catalog_search_title_tin", "tin"),
    ]


async def test_a_title_search_can_use_the_title_index(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        row[0]
        for row in await db_session.execute(
            text("EXPLAIN SELECT catalog_id FROM catalog_search WHERE title ==> 'paris'")
        )
    )

    assert "ix_catalog_search_title_tin" in plan, plan


@pytest.mark.parametrize(
    ("chunks", "typo_tolerant", "expected"),
    [
        # Under 4 letters exact, 4-5 letters one edit, 6+ two; the last word also as a prefix.
        (["paris"], True, "paris~1 OR paris*"),
        (["Bibliothèque", "lib"], True, "bibliotheque~2 OR lib OR lib*"),
        (["de", "paris"], True, "de OR paris~1 OR paris*"),
        # The prefix goes on the last chunk that has a word: trailing punctuation keeps it.
        (["guten", "!!!"], True, "guten~1 OR guten*"),
        # A trailing phrase is never a prefix, and the word before it is not one either.
        (["guten", '"de paris"'], True, 'guten~1 OR "de paris"'),
        # Folded by `fold()` first: TIN's own tokenizer would keep ß.
        (["Straße"], True, "strasse~2 OR strasse*"),
        # A quoted chunk is a phrase, exact, and never a prefix, even of one word.
        (['"numérique de paris"'], True, '"numerique de paris"'),
        (['"paris"'], True, '"paris"'),
        # A bare chunk the tokenizer splits is a phrase too; an apostrophe does not split.
        (["ile-de-france"], True, '"ile de france"'),
        (["l'école"], True, "l'ecole~2 OR l'ecole*"),
        # Inside a phrase `_` is a gap unless escaped.
        (['"a_b c"'], True, '"a\\_b c"'),
        # Exclusions are exact.
        (["paris", '"de bruxelles"'], False, 'paris OR "de bruxelles"'),
        # Folding lowercases, so an UPPER CASE keyword becomes an ordinary word.
        (["AND"], True, "and OR and*"),
        # TINQL syntax characters never reach the query.
        (["x~2^3*?"], True, '"x 2 3"'),
        (["(a)", "[b]"], True, "a OR b OR b*"),
    ],
)
async def test_the_query_built_from_chunks(
    db_session: AsyncSession, chunks: list[str], typo_tolerant: bool, expected: str
) -> None:
    built = await db_session.scalar(
        text("SELECT public.registry_tinql(CAST(:c AS text[]), :t)"),
        {"c": chunks, "t": typo_tolerant},
    )

    assert built == expected


@pytest.mark.parametrize("chunks", [[], ["!!!"], ["& | ! :"], ["'", "-"]])
async def test_chunks_with_no_word_build_no_query(
    db_session: AsyncSession, chunks: list[str]
) -> None:
    """NULL, so `==>` matches nothing instead of the whole table or a parse error."""
    built = await db_session.scalar(
        text("SELECT public.registry_tinql(CAST(:c AS text[]), true)"), {"c": chunks}
    )

    assert built is None


class NoticeRecorder:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, _connection: Any, message: Any) -> None:
        self.messages.append(str(message))


async def record_notices(connection: AsyncConnection, run: Callable[[], Any]) -> list[str]:
    raw = await connection.get_raw_connection()
    driver = raw.driver_connection
    assert driver is not None
    recorder = NoticeRecorder()
    driver.add_log_listener(recorder)
    try:
        await run()
    finally:
        driver.remove_log_listener(recorder)
    return recorder.messages


@pytest.mark.parametrize("query", ["& | ! :", "!!!", "'", "paris & | !", "-& paris"])
async def test_punctuation_only_chunks_raise_no_notice(
    db_connection: AsyncConnection,
    db_session: AsyncSession,
    searchable_catalogs: None,
    query: str,
) -> None:
    messages = await record_notices(db_connection, lambda: search_rows(db_session, query))

    assert messages == []
