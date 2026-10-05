"""What the trigram half can and cannot find, measured instead of assumed (ADR-051).

The hand-picked typo rows in the search table prove a handful of cases work. They say nothing
about *how far* the fuzziness reaches, so these tests derive it from first principles: take every
word the data is findable by, damage it in each of the four ways a person mistypes (drop a letter,
add one, replace one, swap two neighbours), and count how often the right catalog still comes back.
The result is an envelope with floors under it, plus the converse: words unrelated to anything in
the data must return nothing.

Why the numbers are what they are: a word of n letters has about n + 2 trigrams (the word is padded
with spaces). Dropping or replacing one letter breaks about three of them, swapping two neighbours
breaks about four, and `<%` needs `word_similarity` of at least 0.6. A long word survives one edit;
a short one does not. The floors pin the measured behaviour so loosening or tightening the
threshold (`pg_trgm.word_similarity_threshold`) is a visible, deliberate change.

The application runs at 0.5 (`WORD_SIMILARITY_THRESHOLD`, chosen 6 October 2026 against these
numbers); PostgreSQL's own default is 0.6. Measured on the 12-catalog seed:

    edit          at 0.6      at 0.5 (now)   at 0.4
    drop          79%         96%            99%
    add           75%         96%            100%
    replace       61%         90%            97%
    swap          24%         66%            83%
    unrelated words returned (of 40):   0    1 (library)     3
"""

import collections
import unicodedata

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from registry.db.session import build_session_factory, read_only_transaction
from registry.repositories.search_repository import WORD_SIMILARITY_THRESHOLD
from tests.search_helpers import search_rows

pytestmark = pytest.mark.integration

#: Words the seed is findable by, each with a fragment of the title of a catalog that holds them
#: (in its title, its country names or its subdivision names).
TARGETS = [
    ("gutenberg", "Gutenberg"),
    ("librivox", "Librivox"),
    ("standard", "Standard Ebooks"),
    ("ebooks", "Standard Ebooks"),
    ("lirtuel", "Lirtuel"),
    ("openbare", "Openbare"),
    ("vlaanderen", "Openbare"),
    ("mediatheque", "Valais"),
    ("valais", "Valais"),
    ("romande", "Romande"),
    ("numerique", "Paris"),
    ("paris", "Paris"),
    ("belgique", "Lirtuel"),
    ("belgium", "Lirtuel"),
    ("bruxelles", "Lirtuel"),
    ("wallonie", "Lirtuel"),
    ("suisse", "Valais"),
    ("switzerland", "Valais"),
    ("france", "Paris"),
    ("liber", "Liber Liber"),
    ("bibliotheque", "Paris"),
    ("flanders", "Openbare"),
    ("vallese", "Valais"),
]

#: Ordinary words with no connection to any catalog. None may bring anything back at the default
#: threshold; a word that does is noise a reader would see.
UNRELATED = [
    "bristol", "kitchen", "qwerty", "telephone", "zzzzqqq", "computer", "amsterdam", "london",
    "tokyo", "guitar", "orange", "bridge", "castle", "mountain", "river", "music", "history",
    "science", "poetry", "novel", "comic", "garden", "window", "yellow", "silver", "market",
    "bakery", "cinema", "museum", "harbour", "coffee", "winter", "summer", "school",
    "holland", "station", "hospital", "pasta", "zwitserland", "kestrel",
]  # fmt: skip

#: Under the measured recall at 0.5 (see the module docstring), so a real regression fails and
#: ordinary variation in the data does not. Swapped neighbours are the weak spot; the floor
#: says so.
RECALL_FLOORS = {"drop": 0.90, "add": 0.90, "replace": 0.80, "swap": 0.55}


def damage(word: str) -> dict[str, set[str]]:
    """Every single mistyping of *word*, by kind. 'x' is the replacement and the added letter."""
    out: dict[str, set[str]] = collections.defaultdict(set)
    for i in range(len(word)):
        out["drop"].add(word[:i] + word[i + 1 :])
        out["replace"].add(word[:i] + "x" + word[i + 1 :])
        if i < len(word) - 1 and word[i] != word[i + 1]:
            out["swap"].add(word[:i] + word[i + 1] + word[i] + word[i + 2 :])
    for i in range(len(word) + 1):
        out["add"].add(word[:i] + "x" + word[i:])
    return {kind: {v for v in variants if len(v) >= 2} for kind, variants in out.items()}


async def finds(
    session: AsyncSession,
    query: str,
    title_fragment: str,
    threshold: float = WORD_SIMILARITY_THRESHOLD,
) -> bool:
    rows = await search_rows(session, query, threshold=threshold)
    return any(title_fragment in title for title, _tier, _score in rows)


async def recall(
    session: AsyncSession,
    kind: str,
    *,
    min_length: int = 0,
    max_length: int = 99,
    threshold: float = WORD_SIMILARITY_THRESHOLD,
) -> tuple[float, list[str]]:
    """Share of damaged variants that still find their catalog, and the ones that did not."""
    hits, total, missed = 0, 0, []
    for word, fragment in TARGETS:
        if not min_length <= len(word) <= max_length:
            continue
        for variant in sorted(damage(word)[kind]):
            total += 1
            if await finds(session, variant, fragment, threshold):
                hits += 1
            else:
                missed.append(f"{word}>{variant}")
    return hits / total, missed


# --- the targets themselves are sound ----------------------------------------------------------


async def test_every_target_word_finds_its_catalog_when_spelled_correctly(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """The control for everything below: a recall number only means something if the undamaged
    word is found every time, otherwise the target list is measuring itself."""
    missed = [w for w, fragment in TARGETS if not await finds(db_session, w, fragment)]

    assert missed == []


# --- recall: how far the fuzziness reaches -----------------------------------------------------


@pytest.mark.parametrize(("kind", "floor"), sorted(RECALL_FLOORS.items()))
async def test_a_single_typo_still_finds_the_catalog_at_least_this_often(
    db_session: AsyncSession, searchable_catalogs: None, kind: str, floor: float
) -> None:
    rate, missed = await recall(db_session, kind)

    assert rate >= floor, f"{kind}: {rate:.0%} < {floor:.0%}; missed e.g. {missed[:12]}"


async def test_adding_a_letter_to_a_long_word_never_loses_it(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """A guarantee, not a rate: inserting a letter leaves every original trigram except the one it
    splits, so for a word of nine letters or more the similarity stays above 0.6 whatever the
    letter and wherever it lands."""
    rate, missed = await recall(db_session, "add", min_length=9)

    assert missed == [], missed
    assert rate == 1.0


async def test_dropping_a_letter_from_a_long_word_is_found_in_most_cases(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    rate, missed = await recall(db_session, "drop", min_length=9)

    assert rate >= 0.80, f"{rate:.0%}; missed {missed[:10]}"


async def test_short_words_are_forgiven_less_than_long_ones(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """The principle behind the numbers: a damaged short word shares too few trigrams with the
    real one. If this stops holding the threshold has stopped doing its job as a length-aware
    filter, and the unrelated-word test below should be the first thing to look at."""
    short, _ = await recall(db_session, "replace", max_length=6)
    long_, _ = await recall(db_session, "replace", min_length=9)

    assert short < long_, f"short {short:.0%} vs long {long_:.0%}"


# --- precision: what the fuzziness must not drag in --------------------------------------------


@pytest.mark.parametrize("word", UNRELATED)
async def test_a_word_unrelated_to_anything_in_the_data_returns_nothing(
    db_session: AsyncSession, searchable_catalogs: None, word: str
) -> None:
    assert await search_rows(db_session, word) == []


async def test_the_unrelated_word_check_can_fail(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """The test of the test: loosen the threshold and the same list must start returning things,
    otherwise a green precision check would prove nothing about this threshold."""
    noisy = [w for w in UNRELATED if await search_rows(db_session, w, threshold=0.3)]

    assert len(noisy) >= 2, noisy


async def test_the_recall_check_can_fail(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """The mirror: tighten the threshold and recall must drop under its floor."""
    rate, _ = await recall(db_session, "drop", threshold=0.9)

    assert rate < RECALL_FLOORS["drop"], f"{rate:.0%}"


async def test_the_one_unrelated_word_that_gets_through_is_a_known_and_accepted_trade(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """At 0.5 `library` brings back the catalogs named after the French and Italian words for it
    (Liber Liber, libres, Librivox, Bibliothèque). Accepted on 6 October: they follow every exact
    match and a reader looking for a library would welcome them. Pinned so that a second word
    joining it, or this one changing, is noticed and decided."""
    rows = await search_rows(db_session, "library")

    assert rows, "library no longer matches anything: the threshold moved, update the docstring"
    assert {tier for _title, tier, _score in rows} == {2}


async def test_the_application_applies_its_threshold_per_transaction_and_does_not_leak_it(
    migrated_database: str,
) -> None:
    """The server keeps pg_trgm's default of 0.6; only the search transaction runs at 0.5.
    A role or database level override would change every result silently, so the default is
    pinned as well, and the setting must be gone once the transaction ends."""
    engine = create_async_engine(migrated_database, pool_size=1, max_overflow=0)
    factory = build_session_factory(engine)
    show = text("SHOW pg_trgm.word_similarity_threshold")
    settings = {"pg_trgm.word_similarity_threshold": str(WORD_SIMILARITY_THRESHOLD)}
    try:
        async with engine.connect() as connection:
            # The setting only exists once the extension's library is loaded in the session,
            # which its first function call does.
            await connection.execute(text("SELECT public.word_similarity('a', 'a')"))
            default = (await connection.execute(show)).scalar_one()
        async with read_only_transaction(
            factory, statement_timeout_ms=1000, local_settings=settings
        ) as session:
            inside = (await session.execute(show)).scalar_one()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT public.word_similarity('a', 'a')"))
            after = (await connection.execute(show)).scalar_one()
    finally:
        await engine.dispose()

    assert WORD_SIMILARITY_THRESHOLD == 0.5
    assert (default, inside, after) == ("0.6", "0.5", "0.6")


# --- invariants of the result ------------------------------------------------------------------

MIXED_QUERIES = [
    "bibliotheque",
    "bibliotheqe",
    "belgum",
    "bruxels",
    "gutenbrg",
    "standard ebooks",
    "stndard ebooks",
    "paris valais",
    "parix valais",
    "numérique de paris",
    "liber",
    "libr",
]


@pytest.mark.parametrize("query", MIXED_QUERIES)
async def test_exact_matches_always_come_before_fuzzy_ones(
    db_session: AsyncSession, searchable_catalogs: None, query: str
) -> None:
    tiers = [tier for _title, tier, _score in await search_rows(db_session, query)]

    assert tiers == sorted(tiers), tiers


@pytest.mark.parametrize("query", MIXED_QUERIES)
async def test_a_catalog_is_listed_once_even_when_both_halves_match_it(
    db_session: AsyncSession, searchable_catalogs: None, query: str
) -> None:
    titles = [title for title, _tier, _score in await search_rows(db_session, query)]

    assert len(titles) == len(set(titles)), titles


@pytest.mark.parametrize(
    ("typed", "same_as"),
    [
        ("BRUXELS", "bruxels"),
        ("Bruxéls", "bruxels"),
        ("GUTENBRG", "gutenbrg"),
        ("Bibliothèqe", "bibliotheqe"),
        ("Belgüm", "belgum"),
    ],
)
async def test_capitals_and_accents_do_not_change_a_fuzzy_result(
    db_session: AsyncSession, searchable_catalogs: None, typed: str, same_as: str
) -> None:
    """Folding happens in the database for typed and stored text alike (ADR-059)."""
    folded = unicodedata.normalize("NFD", typed).encode("ascii", "ignore").decode().lower()
    assert folded == same_as, "the pair must be the same word once folded"

    assert await search_rows(db_session, typed) == await search_rows(db_session, same_as)


async def test_a_fuzzy_match_is_always_a_word_the_catalog_actually_holds_nearly(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """Every tier-2 result must really contain something similar to what was typed. This is the
    definition of the half, checked against the stored text with the same function: no result may
    sit below the threshold. It also relies on each of these typos matching only by trigrams."""
    for typed in ("bruxels", "belgum", "gutenbrg", "bibliotheqe", "walis"):
        results = await search_rows(db_session, typed)
        assert results, typed
        for title, tier, _score in results:
            if tier != 2:
                continue
            similarity = (
                await db_session.execute(
                    text(
                        "SELECT word_similarity(public.fold(:q), cs.names) FROM catalog_search cs "
                        "JOIN catalogs c ON c.id = cs.catalog_id WHERE c.title = :t"
                    ),
                    {"q": typed, "t": title},
                )
            ).scalar_one()
            assert similarity >= WORD_SIMILARITY_THRESHOLD, (typed, title, similarity)
