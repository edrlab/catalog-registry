"""The search table of the test plan (section 1), one parametrized case per row.

Expected results are (title, tier) in rank order, measured on 1 October with the real
migrations and the two data files. Tier 1 is a word match, tier 2 a trigram-only match, which
always follows every word match (ADR-051). Where the plan calls a result known and accepted the
case says so and pins the CURRENT behaviour, so a change is noticed and has to be decided
rather than slipping in.

The cases are plain data (query, expected) on purpose: plan section 6 wants this table to become
a search-quality score one day.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.cli.seed import import_catalog_document
from registry.domain.search_query import parse_search_query
from registry.repositories.search_repository import CatalogSearchRepository
from tests.search_cases import CASES, LIRTUEL, OPENBARE, PARIS, trigrams, words
from tests.search_helpers import ALL_TITLES, search_rows

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(("query", "expected"), CASES, ids=[f"{q or '(empty)'}" for q, _ in CASES])
async def test_a_search_returns_the_planned_catalogs_in_the_planned_order(
    db_session: AsyncSession,
    searchable_catalogs: None,
    query: str,
    expected: list[tuple[str, int]],
) -> None:
    rows = await search_rows(db_session, query)

    assert [(title, tier) for title, tier, _ in rows] == expected


def test_every_title_the_table_names_is_in_a_data_file() -> None:
    named = {title for _, expected in CASES for title, _ in expected}

    assert named <= set(ALL_TITLES), named - set(ALL_TITLES)


@pytest.mark.parametrize(
    ("query", "title", "score"),
    [
        ("Brussel", OPENBARE, 0.638),
        ("Brussel", LIRTUEL, 0.122),
        ("Belgique -lirtuel", OPENBARE, 0.243),
        ('"numérique de paris"', PARIS, 1.0),
        ("Paris", PARIS, 0.638),
    ],
)
async def test_the_scores_the_plan_pins(
    db_session: AsyncSession, searchable_catalogs: None, query: str, title: str, score: float
) -> None:
    """With the weights (0.1, 0.2, 0.4, 1.0). `Paris` is 0.638 since the city was added
    (0.608 before): the title match plus the city at label C."""
    rows = {t: s for t, _, s in await search_rows(db_session, query)}

    assert rows[title] == pytest.approx(score, abs=0.0006)


async def test_a_negation_keeps_a_real_score(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """`ts_rank` returns 0 for a tsquery containing NOT; ranking uses the positives only."""
    rows = await search_rows(db_session, "Belgique -lirtuel")

    assert [score for _, _, score in rows] != [0.0]


# French overseas, synthetic catalogs until real ones exist (plan: "French overseas"). The
# subdivisions and names below are what the generator's overseas rule produces for them: CLDR
# 48.2 has no subdivision names for the letter-coded ones, so they come from the territory.

OVERSEAS_NC = "Overseas catalog alpha"
OVERSEAS_GF = "Overseas catalog beta"


async def add_overseas_catalog(session: AsyncSession, title: str, code: str) -> None:
    document = {
        "metadata": {"title": title, "kind": ["public"], "country": "FR", "subdivisions": [code]},
        "links": [
            {
                "href": f"https://overseas.example/{code}",
                "type": "application/opds+json",
                "rel": "catalog",
            }
        ],
    }
    await import_catalog_document(session, document, recommended=False)
    await session.flush()


@pytest.fixture
async def overseas_catalogs(db_session: AsyncSession, searchable_catalogs: None) -> None:
    await db_session.execute(
        text(
            "INSERT INTO subdivisions (code, country_alpha2, subdivision_type) VALUES "
            "('FR-NC', 'FR', 'overseas collectivity with special status'), "
            "('FR-973', 'FR', 'overseas unique territorial collectivity')"
        )
    )
    await db_session.execute(
        text(
            "INSERT INTO subdivision_names (subdivision_code, language_tag, name) VALUES "
            "('FR-NC', 'fr', 'Nouvelle-Calédonie'), ('FR-NC', 'en', 'New Caledonia'), "
            "('FR-973', 'fr', 'Guyane'), ('FR-973', 'en', 'French Guiana')"
        )
    )
    await add_overseas_catalog(db_session, OVERSEAS_NC, "FR-NC")
    await add_overseas_catalog(db_session, OVERSEAS_GF, "FR-973")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Nouvelle-Calédonie", words(OVERSEAS_NC)),
        ("New Caledonia", words(OVERSEAS_NC)),
        ("nouvelle caledonie", words(OVERSEAS_NC)),
        ("Guyane", words(OVERSEAS_GF)),
        ("French Guiana", words(OVERSEAS_GF)),
        ("guyanne", trigrams(OVERSEAS_GF)),
    ],
)
async def test_overseas_territories_are_found_under_country_fr(
    db_session: AsyncSession,
    overseas_catalogs: None,
    query: str,
    expected: list[tuple[str, int]],
) -> None:
    rows = await search_rows(db_session, query)

    assert [(title, tier) for title, tier, _ in rows] == expected


async def test_france_finds_the_paris_library_and_both_overseas_catalogs(
    db_session: AsyncSession, overseas_catalogs: None
) -> None:
    rows = await search_rows(db_session, "France")

    assert {title for title, _, _ in rows} == {PARIS, OVERSEAS_NC, OVERSEAS_GF}
    assert {tier for _, tier, _ in rows} == {1}


# Plan section 2: paging the real data in pages of two.


async def test_pages_of_two_reproduce_the_single_page_order_with_no_duplicate_and_no_gap(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    query = parse_search_query("bibliotheque ebooks liber")
    repository = CatalogSearchRepository(db_session)

    whole = await repository.search_catalogs(query, limit=50, offset=0)
    pages = [await repository.search_catalogs(query, limit=2, offset=n * 2) for n in range(4)]

    paged = [c.title for page in pages for c in page.catalogs]
    assert paged == [c.title for c in whole.catalogs]
    assert len(paged) == len(set(paged)) == whole.total == 6
    assert [len(page.catalogs) for page in pages] == [2, 2, 2, 0]


async def test_a_page_past_the_end_still_reports_the_total_of_six(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    page = await CatalogSearchRepository(db_session).search_catalogs(
        parse_search_query("bibliotheque ebooks liber"), limit=2, offset=6
    )

    assert (page.total, page.catalogs) == (6, ())


async def test_a_title_match_outranks_a_country_match_which_outranks_a_city_match(
    db_session: AsyncSession,
) -> None:
    """The weight order (ADR-052, Q3): title 1.0, country 0.4, subdivisions and city 0.2.

    The data files have no pair of catalogs that differ only in which field matched, so the
    order was invisible to the table. Here the best match is the OLDEST and the weakest the
    NEWEST, so `created_at` cannot be what puts them in order. Switching to the meeting-notes
    order (0.1, 0.4, 0.2, 1.0) swaps the last two and this fails.
    """

    def document(title: str, country: str, city: str | None, href: str) -> dict[str, object]:
        metadata: dict[str, object] = {"title": title, "kind": ["public"], "country": country}
        if city:
            metadata["city"] = city
        return {
            "metadata": metadata,
            "links": [{"href": href, "type": "application/opds+json", "rel": "catalog"}],
        }

    for stamp, doc in [
        (1, document("Belgique reading room", "CH", None, "https://weights.example/title")),
        (2, document("Alpha shelf", "BE", None, "https://weights.example/country")),
        (3, document("Beta shelf", "CH", "Belgique", "https://weights.example/city")),
    ]:
        await import_catalog_document(
            db_session, doc, recommended=False, ordered_at=datetime(2031, 1, stamp, tzinfo=UTC)
        )
    await db_session.flush()

    rows = await search_rows(db_session, "belgique")

    assert [title for title, _, _ in rows] == [
        "Belgique reading room",
        "Alpha shelf",
        "Beta shelf",
    ]
    scores = [score for _, _, score in rows]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[1] > scores[2]
