"""The v0.2 reference data and the schema it lives in, against the migrated database.

The numbers come from ADR-053 (CLDR 48.2: 335 rows over 245 countries) and ADR-055 (the
starting subdivision types). They are properties of the pinned data, so a regenerated snapshot
that changes them is a decision to make, not a typo to fix.
"""

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from registry.db.base import Base
from registry.db.models import *  # noqa: F403 - registers every model on Base.metadata

pytestmark = pytest.mark.integration

#: ADR-055, France as Hadrien listed it on 2 October, plus the type he asked to add for
#: Martinique and Guyane. Left out on purpose: overseas territory, dependency, European
#: collectivity.
FRANCE_TYPES = {
    "metropolitan region",
    "metropolitan department",
    "metropolitan collectivity with special status",
    "overseas departmental collectivity",
    "overseas collectivity",
    "overseas collectivity with special status",
    "overseas unique territorial collectivity",
}

#: Countries CLDR gives no language to; they get English only (ADR-053).
NO_LANGUAGE = {"AQ", "BV", "HM", "TF"}


async def scalar(session: AsyncSession, statement: str) -> int:
    return int((await session.scalar(text(statement))) or 0)


async def rows(session: AsyncSession, statement: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in await session.execute(text(statement))]


async def test_country_languages_has_335_rows_over_245_countries(
    db_session: AsyncSession,
) -> None:
    assert await scalar(db_session, "SELECT count(*) FROM country_languages") == 335
    assert (
        await scalar(db_session, "SELECT count(DISTINCT country_code) FROM country_languages")
        == 245
    )


async def test_the_countries_without_an_official_language_have_none(
    db_session: AsyncSession,
) -> None:
    with_languages = {
        r[0] for r in await rows(db_session, "SELECT DISTINCT country_code FROM country_languages")
    }
    all_countries = {r[0] for r in await rows(db_session, "SELECT alpha2 FROM countries")}

    assert all_countries - with_languages == NO_LANGUAGE
    assert await scalar(db_session, "SELECT count(*) FROM countries") == 249


@pytest.mark.parametrize("table", ["country_languages", "country_names", "subdivision_names"])
async def test_every_language_tag_is_lowercase(db_session: AsyncSession, table: str) -> None:
    assert await scalar(db_session, f"SELECT count(*) FROM {table}") > 0
    assert (
        await scalar(
            db_session, f"SELECT count(*) FROM {table} WHERE language_tag <> lower(language_tag)"
        )
        == 0
    )


async def test_an_uppercase_language_tag_is_refused_by_the_database(
    db_session: AsyncSession,
) -> None:
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    nested = await db_session.begin_nested()
    with pytest.raises(IntegrityError, match="language_tag_lowercase"):
        await db_session.execute(text("INSERT INTO country_languages VALUES ('FR', 'FR')"))
    await nested.rollback()


async def test_country_names_exist_only_in_english_or_an_official_language_of_the_country(
    db_session: AsyncSession,
) -> None:
    stray = await rows(
        db_session,
        "SELECT country_code, language_tag FROM country_names n WHERE language_tag <> 'en' "
        "AND NOT EXISTS (SELECT 1 FROM country_languages l WHERE l.country_code = n.country_code "
        "AND l.language_tag = n.language_tag) ORDER BY 1, 2",
    )

    assert stray == []


async def test_every_country_has_an_english_name_except_possibly_none(
    db_session: AsyncSession,
) -> None:
    missing = await rows(
        db_session,
        "SELECT alpha2 FROM countries c WHERE NOT EXISTS "
        "(SELECT 1 FROM country_names n WHERE n.country_code = c.alpha2 AND n.language_tag = 'en')",
    )

    assert missing == []


@pytest.mark.parametrize(
    ("country", "languages"),
    [
        ("BE", {"nl", "fr", "de"}),  # Belgium keeps German (ADR-053)
        ("CH", {"de", "fr", "it"}),  # Romansh is official_regional and left out
        ("FR", {"fr"}),
        ("CA", {"en", "fr"}),
    ],
)
async def test_the_official_languages_of_the_countries_hadrien_checked(
    db_session: AsyncSession, country: str, languages: set[str]
) -> None:
    found = {
        r[0]
        for r in await rows(
            db_session,
            f"SELECT language_tag FROM country_languages WHERE country_code = '{country}'",
        )
    }

    assert found == languages


async def test_belgium_has_names_in_german(db_session: AsyncSession) -> None:
    assert (
        await scalar(
            db_session,
            "SELECT count(*) FROM country_names WHERE country_code = 'BE' AND language_tag = 'de'",
        )
        >= 1
    )


async def test_fr_idf_has_english_and_french_names(db_session: AsyncSession) -> None:
    languages = {
        r[0]
        for r in await rows(
            db_session,
            "SELECT DISTINCT language_tag FROM subdivision_names WHERE subdivision_code = 'FR-IDF'",
        )
    }

    assert {"en", "fr"} <= languages


async def test_every_seeded_subdivision_has_at_least_one_name(db_session: AsyncSession) -> None:
    nameless = await rows(
        db_session,
        "SELECT code FROM subdivisions s WHERE NOT EXISTS "
        "(SELECT 1 FROM subdivision_names n WHERE n.subdivision_code = s.code)",
    )

    assert nameless == []
    assert await scalar(db_session, "SELECT count(*) FROM subdivisions") >= 5


async def test_subdivision_names_are_only_loaded_for_known_subdivisions(
    db_session: AsyncSession,
) -> None:
    assert (
        await scalar(
            db_session,
            "SELECT count(*) FROM subdivision_names n WHERE NOT EXISTS "
            "(SELECT 1 FROM subdivisions s WHERE s.code = n.subdivision_code)",
        )
        == 0
    )


async def test_subdivision_names_follow_the_countrys_languages_or_english(
    db_session: AsyncSession,
) -> None:
    """A name in a language the country does not have is stored but never indexed; the
    generator only produces official languages and English, so none should exist."""
    stray = await rows(
        db_session,
        "SELECT subdivision_code, language_tag FROM subdivision_names n "
        "WHERE language_tag <> 'en' AND NOT EXISTS (SELECT 1 FROM country_languages l "
        "WHERE l.country_code = left(n.subdivision_code, 2) AND l.language_tag = n.language_tag)",
    )

    assert stray == []


async def test_france_has_exactly_the_seven_types_of_adr_055(db_session: AsyncSession) -> None:
    found = {
        r[0]
        for r in await rows(
            db_session,
            "SELECT subdivision_type FROM country_subdivision_types WHERE country_code = 'FR'",
        )
    }

    assert found == FRANCE_TYPES


@pytest.mark.parametrize(
    ("country", "types"),
    [
        ("BE", {"region", "province"}),
        ("CH", {"canton"}),
        ("CA", {"province", "territory"}),
    ],
)
async def test_the_other_starting_countries_have_their_adr_055_types(
    db_session: AsyncSession, country: str, types: set[str]
) -> None:
    found = {
        r[0]
        for r in await rows(
            db_session,
            "SELECT subdivision_type FROM country_subdivision_types "
            f"WHERE country_code = '{country}'",
        )
    }

    assert found == types


async def test_all_subdivision_types_are_lowercase(db_session: AsyncSession) -> None:
    assert (
        await scalar(
            db_session,
            "SELECT count(*) FROM country_subdivision_types "
            "WHERE subdivision_type <> lower(subdivision_type)",
        )
        == 0
    )
    assert (
        await scalar(
            db_session,
            "SELECT count(*) FROM subdivisions WHERE subdivision_type <> lower(subdivision_type)",
        )
        == 0
    )


async def test_the_lowercase_rule_is_enforced_by_a_constraint(db_session: AsyncSession) -> None:
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    nested = await db_session.begin_nested()
    with pytest.raises(IntegrityError, match="subdivision_type_lowercase"):
        await db_session.execute(
            text("INSERT INTO country_subdivision_types VALUES ('FR', 'Region')")
        )
    await nested.rollback()


@pytest.mark.parametrize(
    ("code", "stored_type"),
    [
        ("BE-BRU", "region"),
        ("BE-WAL", "region"),
        ("BE-VLG", "region"),
        ("CH-VS", "canton"),
        ("FR-IDF", "metropolitan region"),
    ],
)
async def test_a_subdivisions_stored_type_is_a_type_its_country_offers(
    db_session: AsyncSession, code: str, stored_type: str
) -> None:
    result = await rows(
        db_session,
        "SELECT s.subdivision_type, "
        "EXISTS (SELECT 1 FROM country_subdivision_types t "
        "        WHERE t.country_code = s.country_alpha2 "
        "          AND t.subdivision_type = s.subdivision_type) "
        f"FROM subdivisions s WHERE s.code = '{code}'",
    )

    assert result == [(stored_type, True)]


async def test_every_stored_subdivision_type_is_offered_for_its_country(
    db_session: AsyncSession,
) -> None:
    outside = await rows(
        db_session,
        "SELECT code FROM subdivisions s WHERE subdivision_type IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM country_subdivision_types t WHERE t.country_code = s.country_alpha2 "
        "AND t.subdivision_type = s.subdivision_type)",
    )

    assert outside == []


async def test_the_migrated_schema_matches_the_models_alembic_check(
    db_connection: AsyncConnection,
) -> None:
    """`alembic check`, in process: autogenerate against the migrated database finds no diff."""

    def compare(connection: Connection) -> list[object]:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        return list(compare_metadata(context, Base.metadata))

    assert await db_connection.run_sync(compare) == []
