"""Small helpers shared by the search suites. Only what several files need lives here."""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from registry.core.config import Settings
from registry.core.schema_validation import build_schema_validator
from registry.db.session import build_read_engine
from registry.domain.search_query import MAX_RESULTS, parse_search_query
from registry.repositories.search_repository import _SEARCH, SEARCH_CONNECTION_SETTINGS
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
    """The real response body, against the repository's own schema."""
    errors = sorted(build_schema_validator("feed.schema.json").iter_errors(body), key=str)
    assert not errors, [error.message for error in errors]


def assert_valid_catalog(catalog: dict[str, Any]) -> None:
    errors = sorted(build_schema_validator("catalog.schema.json").iter_errors(catalog), key=str)
    assert not errors, [catalog["metadata"]["title"], [e.message for e in errors]]


async def search_rows(
    session: AsyncSession,
    query: str,
    *,
    page: int = 1,
    limit: int = PAGE,
) -> list[tuple[str, int, float]]:
    """(title, tier, score) in rank order, from the repository's own statement.

    The repository returns catalogs only; tier and score are what the table pins, so this runs
    the same `_SEARCH` with the same parameters and joins the titles on afterwards. Tier 1 is a
    match on a word as typed (score above 0), tier 2 a match found only through a typo allowance
    or the last word's prefix, which TIN scores 0, so it always follows tier 1.
    """
    parsed = parse_search_query(query)
    if parsed.is_empty:
        return []
    result = await session.execute(
        _SEARCH,
        {
            "positives": list(parsed.positives),
            "negatives": list(parsed.negatives),
            "limit": limit,
            "offset": (page - 1) * limit,
            "opds": False,
            "count_cap": MAX_RESULTS,
            "no_open": False,
            "no_audiobook": False,
        },
    )
    rows = [r for r in result.all() if r.catalog_id is not None]
    titles = await titles_by_id(session, [r.catalog_id for r in rows])
    return [(titles[r.catalog_id], 1 if r.score > 0 else 2, float(r.score)) for r in rows]


async def titles_by_id(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        text("SELECT id, title FROM catalogs WHERE id = ANY(:ids)"), {"ids": ids}
    )
    return {row.id: row.title for row in rows}


async def catalog_search_row(session: AsyncSession, title: str) -> dict[str, Any] | None:
    """The stored search row of the catalog with this title, or None."""
    row = (
        await session.execute(
            text(
                "SELECT cs.title, cs.city, cs.places, cs.has_opds_catalog, cs.is_open, "
                "cs.audiobook_only FROM catalog_search cs "
                "JOIN catalogs c ON c.id = cs.catalog_id WHERE c.title = :t"
            ),
            {"t": title},
        )
    ).first()
    return None if row is None else dict(row._mapping)


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
async def search_engine_with(settings: Settings, **overrides: str) -> AsyncIterator[AsyncEngine]:
    """The production search pool (`build_read_engine`), with some server settings replaced.

    A short `statement_timeout` makes a lock-blocked search fail in 150 ms instead of 1 s; an
    `application_name` lets a test find (and terminate) exactly this engine's backends.
    """
    engine = build_read_engine(settings, {**SEARCH_CONNECTION_SETTINGS, **overrides})
    try:
        yield engine
    finally:
        await engine.dispose()


@asynccontextmanager
async def committed_catalogs(
    url: str, documents: list[dict[str, Any]], *, recommended: bool = False
) -> AsyncIterator[list[uuid.UUID]]:
    """Really commit catalogs, for tests whose app has its own pools (a rollback fixture's
    transaction is invisible to them). Deleted by id in a `finally`; the children and the search
    row go with them (foreign keys cascade)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

    from registry.cli.seed import import_catalog_document  # noqa: PLC0415

    engine = create_async_engine(url)
    ids: list[uuid.UUID] = []
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            for document in documents:
                catalog, _ = await import_catalog_document(
                    session, document, recommended=recommended
                )
                ids.append(catalog.id)
            await session.commit()
        yield ids
    finally:
        async with engine.begin() as cleanup:
            await cleanup.execute(text("DELETE FROM catalogs WHERE id = ANY(:ids)"), {"ids": ids})
        await engine.dispose()
