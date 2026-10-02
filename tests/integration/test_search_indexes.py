"""Index use (plan section 5) and the NOTICE trap (plan section 4).

The data set is far too small for the planner to pick a GIN index on cost, so sequential scans
are disabled for the transaction: the question is whether each half *can* use its index, which
is what keeps it sublinear at tens of thousands of catalogs.
"""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from tests.search_helpers import search_rows

pytestmark = pytest.mark.integration


async def explain(session: AsyncSession, statement: str) -> str:
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    rows = await session.execute(text(f"EXPLAIN {statement}"))
    return "\n".join(row[0] for row in rows)


async def test_the_word_half_uses_the_gin_index_on_the_document(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    plan = await explain(
        db_session,
        "SELECT catalog_id FROM catalog_search "
        "WHERE document @@ public.registry_query(ARRAY['paris'], ARRAY[]::text[])",
    )

    assert "ix_catalog_search_document" in plan, plan


async def test_the_trigram_half_uses_the_gin_index_on_the_names(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    plan = await explain(
        db_session,
        "SELECT catalog_id FROM catalog_search WHERE public.fold('bruxels') <% names",
    )

    assert "ix_catalog_search_names_trgm" in plan, plan


async def test_similarity_alone_would_not_use_the_trigram_index(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """Why the statement filters with `<%`: `word_similarity(...) > x` cannot use the index."""
    plan = await explain(
        db_session,
        "SELECT catalog_id FROM catalog_search WHERE word_similarity('bruxels', names) > 0.6",
    )

    assert "ix_catalog_search_names_trgm" not in plan, plan


class NoticeRecorder:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, _connection: Any, message: Any) -> None:
        self.messages.append(str(message))


async def record_notices(connection: AsyncConnection, run: Callable[[], Any]) -> list[str]:
    raw = await connection.get_raw_connection()
    driver = raw.driver_connection
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


async def test_the_notice_trap_is_real_for_the_unguarded_function(
    db_connection: AsyncConnection, db_session: AsyncSession
) -> None:
    """The guard is what `registry_query` adds: calling `websearch_to_tsquery` on punctuation
    directly does notify, so a test that sees no notice above is not blind."""
    messages = await record_notices(
        db_connection,
        lambda: db_session.execute(
            text("SELECT websearch_to_tsquery('public.registry_simple', '& | ! :')")
        ),
    )

    assert any("no lexemes" in m or "stop words" in m for m in messages), messages
