"""Small helpers shared by the search suites. Only what several files need lives here."""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from registry.core.schema_validation import build_schema_validator
from registry.domain.search_query import parse_search_query
from registry.repositories.search_repository import _SEARCH, RANK_WEIGHTS
from tests.conftest import LIBRARIES_FILE, SEED_FILE

PAGE = 50

#: Longer than any statement timeout a test sets, shorter than a hung CI job.
HANG_GUARD_SECONDS = 8


def titles_in(path: Any) -> list[str]:
    return [
        c["metadata"]["title"] for c in json.loads(path.read_text(encoding="utf-8"))["catalogs"]
    ]


#: Derived from the two data files. The expected results in the search table name catalogs by
#: these titles, and `test_search_table` checks each name is still in a file.
RECOMMENDED_TITLES = titles_in(SEED_FILE)
LIBRARY_TITLES = titles_in(LIBRARIES_FILE)
ALL_TITLES = RECOMMENDED_TITLES + LIBRARY_TITLES


def assert_valid_feed(body: dict[str, Any]) -> None:
    """R7: the real response body, against the repository's own schema."""
    errors = sorted(build_schema_validator("feed.schema.json").iter_errors(body), key=str)
    assert not errors, [error.message for error in errors]


def assert_valid_catalog(catalog: dict[str, Any]) -> None:
    errors = sorted(build_schema_validator("catalog.schema.json").iter_errors(catalog), key=str)
    assert not errors, [catalog["metadata"]["title"], [e.message for e in errors]]


async def search_rows(
    session: AsyncSession, query: str, *, page: int = 1, limit: int = PAGE
) -> list[tuple[str, int, float]]:
    """(title, tier, score) in rank order, from the repository's own statement.

    The repository returns catalogs only; tier and score are what the table pins, so this runs
    the same `_SEARCH` with the same parameters and joins the titles on afterwards.
    """
    parsed = parse_search_query(query)
    if parsed.is_empty:
        return []
    result = await session.execute(
        _SEARCH,
        {
            "positives": list(parsed.positives),
            "negatives": list(parsed.negatives),
            "trigram_text": parsed.trigram_text,
            "weights": list(RANK_WEIGHTS),
            "limit": limit,
            "offset": (page - 1) * limit,
        },
    )
    rows = [r for r in result.all() if r.catalog_id is not None]
    titles = await titles_by_id(session, [r.catalog_id for r in rows])
    return [(titles[r.catalog_id], r.tier, float(r.score)) for r in rows]


async def titles_by_id(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        text("SELECT id, title FROM catalogs WHERE id = ANY(:ids)"), {"ids": ids}
    )
    return {row.id: row.title for row in rows}


async def catalog_search_row(session: AsyncSession, title: str) -> dict[str, str] | None:
    """The stored document and names of the catalog with this title, or None."""
    row = (
        await session.execute(
            text(
                "SELECT cs.document::text AS document, cs.names AS names FROM catalog_search cs "
                "JOIN catalogs c ON c.id = cs.catalog_id WHERE c.title = :t"
            ),
            {"t": title},
        )
    ).first()
    return None if row is None else {"document": row.document, "names": row.names}


async def add_synthetic_catalogs(session: AsyncSession, count: int, word: str) -> None:
    """*count* active catalogs titled "<word> shelf N", through the real import path."""
    from registry.cli.seed import import_catalog_document  # noqa: PLC0415

    for n in range(count):
        document = {
            "metadata": {"title": f"{word} shelf {n}", "kind": ["public"]},
            "links": [
                {
                    "href": f"https://{word}.example/{n}",
                    "type": "application/opds+json",
                    "rel": "catalog",
                }
            ],
        }
        await import_catalog_document(session, document, recommended=False)
    await session.flush()


@asynccontextmanager
async def table_locked(url: str) -> AsyncIterator[None]:
    """Hold ACCESS EXCLUSIVE on `catalog_search` from another connection until the block ends."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as locker:
            await locker.execute(text("LOCK TABLE catalog_search IN ACCESS EXCLUSIVE MODE"))
            try:
                yield
            finally:
                await locker.rollback()
    finally:
        await engine.dispose()
