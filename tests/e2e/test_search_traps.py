"""The traps (plan section 4, ADR-059): input that was a real bug or a near miss.

Everything goes through the real ASGI path. Whatever the text, the answer is a 200 and a valid
feed; only the matches differ.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.search_helpers import assert_valid_feed

pytestmark = pytest.mark.e2e

PARIS = "Bibliothèque numérique de Paris"
LIRTUEL = "Lirtuel"
OPENBARE = "De Openbare bibliotheek Vlaanderen en Brussel"
VALAIS = "Médiathèque Valais"
ROMANDE = "Bibliothèque numérique Romande"
RUSSE = "La Bibliothèque russe et slave"


#: Spelled with chr() so no formatter or linter rewrites them into invisible literals.
ZERO_WIDTH_SPACE = chr(0x200B)
BYTE_ORDER_MARK = chr(0xFEFF)
RIGHT_TO_LEFT_OVERRIDE = chr(0x202E)
COMBINING_ACUTE = chr(0x301)
COMBINING_GRAVE = chr(0x300)


async def titles(client: AsyncClient, query: str) -> list[str]:
    response = await client.get("/search", params={"query": query})
    assert response.status_code == 200, (query, response.text)
    body = response.json()
    assert_valid_feed(body)
    return [c["metadata"]["title"] for c in body["catalogs"]]


async def test_a_sharp_s_is_folded_by_the_database_not_by_python(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    """`Suiße` matches `Suisse` because `unaccent` folds ß to ss; Python would leave it."""
    assert await titles(client, "Suiße") == [VALAIS]


@pytest.mark.parametrize(
    "query",
    [
        "& | ! :",
        "&",
        "|",
        "!",
        ":",
        "&&&",
        "!!",
        ":*",
        "<->",
        "((",
        "))",
        "a:b",
        "'",
        "''",
        "\\",
        "\\'",
        "%",
        "_",
        "*",
        "--",
        "/* */",
        "'; DROP TABLE catalogs; --",
        '" OR 1=1 --',
        "paris'); DELETE FROM catalog_search; --",
        "' UNION SELECT title FROM catalogs --",
        "$1",
        "{}",
        "{a,b}",
        "'{\"}'",
    ],
)
async def test_hostile_or_odd_punctuation_never_errors(
    client: AsyncClient, searchable_catalogs: None, query: str
) -> None:
    await titles(client, query)


async def test_a_sql_injection_string_changes_nothing(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    before = (await client.get("/")).json()["metadata"]["numberOfItems"]

    for attack in ("paris'); DROP TABLE catalogs; --", "'; DELETE FROM catalog_search; --"):
        await titles(client, attack)

    assert (await client.get("/")).json()["metadata"]["numberOfItems"] == before
    assert await titles(client, "paris") == [PARIS]


@pytest.mark.parametrize(
    "query",
    [
        "📚",
        "Paris 📚",
        "📚📚📚📚📚📚📚📚",
        "日本語",
        "Ünïcödé",
        "Ø Ł æ œ ß",
        "🇫🇷",
        ZERO_WIDTH_SPACE,
        BYTE_ORDER_MARK + "paris",
        "İstanbul",
        "ǅ",
        "e" + COMBINING_ACUTE + "cole",
        RIGHT_TO_LEFT_OVERRIDE + "paris",
    ],
)
async def test_unicode_and_emoji_input_returns_a_valid_feed(
    client: AsyncClient, searchable_catalogs: None, query: str
) -> None:
    await titles(client, query)


async def test_an_emoji_beside_a_real_word_still_finds_the_word(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    assert await titles(client, "Paris 📚") == [PARIS]


async def test_a_combining_accent_is_folded_like_a_precomposed_one(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    decomposed = f"Me{COMBINING_ACUTE}diathe{COMBINING_GRAVE}que"

    assert await titles(client, decomposed) == [VALAIS]


async def test_a_very_long_single_word_is_cut_and_answers(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    assert await titles(client, "x" * 20_000) == []


async def test_a_long_word_after_a_real_one_still_finds_the_real_one(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    assert await titles(client, "paris " + "x" * 5000) == [PARIS]


async def test_the_256_character_cap_cuts_what_follows(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    assert await titles(client, "x" * 250 + " paris") == [PARIS]  # 256 characters: kept
    assert await titles(client, "x" * 256 + " paris") == []  # the word starts at 257: cut


async def test_the_16_chunk_cap_ignores_the_seventeenth_chunk(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    filler = " ".join(f"qqqq{chr(97 + n)}" for n in range(15))

    assert await titles(client, f"{filler} paris") == [PARIS]  # the 16th chunk: kept
    assert await titles(client, f"{filler} zzzz paris") == []  # the 17th: ignored


async def test_negated_chunks_count_toward_the_16_chunk_cap(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    negations = " ".join(f"-qqqq{chr(97 + n)}" for n in range(16))

    assert await titles(client, f"{negations} paris") == []  # paris is the 17th chunk


async def test_a_negation_past_the_cap_does_not_apply(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    filler = " ".join(f"qqqq{chr(97 + n)}" for n in range(16))

    assert PARIS in await titles(client, f"paris {filler} -paris")


@pytest.mark.parametrize("query", ["OR", "or", "Or OR oR", "OR OR OR"])
async def test_a_bare_or_alone_is_ignored(
    client: AsyncClient, searchable_catalogs: None, query: str
) -> None:
    assert await titles(client, query) == []


async def test_a_bare_or_beside_a_word_changes_nothing(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    assert await titles(client, "OR paris or") == await titles(client, "paris") == [PARIS]


async def test_a_negated_word_removes_the_trigram_only_match_too(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    """`bibliothèques` reaches BnParis only through trigrams; `-paris` must still remove it."""
    assert PARIS in await titles(client, "bibliothèques")

    assert await titles(client, "bibliothèques -paris") == [ROMANDE, RUSSE, OPENBARE]


async def test_a_negated_word_removes_the_word_match_too(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    assert await titles(client, "bibliothèque -paris") == [ROMANDE, RUSSE, OPENBARE]


async def test_a_negated_phrase_removes_only_catalogs_with_the_phrase(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    result = await titles(client, 'bibliothèque -"numérique de paris"')

    assert PARIS not in result
    assert ROMANDE in result  # "numérique" alone is not the phrase


async def test_only_negations_find_nothing(client: AsyncClient, searchable_catalogs: None) -> None:
    assert await titles(client, "-paris -valais") == []


async def test_a_negation_of_an_unknown_word_removes_nothing(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    assert await titles(client, "paris -xyzzy") == [PARIS]


async def test_a_negated_punctuation_chunk_removes_nothing(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    assert await titles(client, "paris -& -!") == [PARIS]


@pytest.mark.parametrize(
    "variants",
    [
        ["Paris", "PARIS", "paris", "pAriS", "pàrïs", "PÀRÏS"],
        ["Bibliothèque", "BIBLIOTHÈQUE", "bibliotheque", "BiBlIoThÈqUe", "bibliothéque"],
        ["Île de France", "ile de france", "ILE DE FRANCE", "ÎLE DE FRANCE", "île de france"],
        ["Île-de-France", "ile-de-france", "ILE-DE-FRANCE", "ÎLE-DE-FRANCE"],
        ["België", "belgie", "BELGIË", "BELGIE"],
        ["Brüssel", "brussel", "BRÜSSEL", "Brussel"],
        ["Suisse", "SUISSE", "suisse"],
        ["Suiße", "suiße", "SUIßE"],
    ],
)
async def test_case_and_accent_variants_return_identical_results(
    client: AsyncClient, searchable_catalogs: None, variants: list[str]
) -> None:
    results = [await titles(client, variant) for variant in variants]

    assert results[0], variants[0]
    assert all(result == results[0] for result in results), dict(
        zip(variants, results, strict=True)
    )


async def test_a_nul_byte_in_the_query_does_not_return_a_server_error(
    app: FastAPI, searchable_catalogs: None
) -> None:
    # `raise_app_exceptions=False`: what a real client sees is the response, not the traceback.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/search", params={"query": "pa\x00ris"})

    assert response.status_code == 200, response.text
    assert_valid_feed(response.json())


@pytest.mark.parametrize("raw", ["%FF", "%ED%A0%80", "%C3", "paris%FF%FEvalais"])
async def test_a_percent_encoded_byte_sequence_that_is_not_utf8_does_not_error(
    client: AsyncClient, searchable_catalogs: None, raw: str
) -> None:
    response = await client.get(f"/search?query={raw}")

    assert response.status_code == 200, response.text
    assert_valid_feed(response.json())


async def test_the_query_parameter_given_twice_uses_one_value_and_answers(
    client: AsyncClient, searchable_catalogs: None
) -> None:
    response = await client.get("/search?query=paris&query=valais")

    assert response.status_code == 200
    assert_valid_feed(response.json())
