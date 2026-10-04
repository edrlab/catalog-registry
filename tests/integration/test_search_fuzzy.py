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
threshold (`pg_trgm.word_similarity_threshold`, 0.6 by default) is a visible, deliberate change.

Measured on the 12-catalog seed with the default threshold:

    edit          recall      at 0.5      at 0.4
    drop          79%         96%         99%
    add           75%         96%         100%
    replace       61%         90%         97%
    swap          24%         66%         83%
    unrelated words returned (of 40):   0           1           3
"""

import collections
import unicodedata

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    "bakery", "cinema", "museum", "harbour", "coffee", "winter", "summer", "library", "school",
    "holland", "station", "hospital", "pasta", "zwitserland", "kestrel",
]  # fmt: skip

#: Under the measured recall (see the module docstring), so a real regression fails and ordinary
#: variation in the data does not. Swapped neighbours are the weak spot and the floor says so.
RECALL_FLOORS = {"drop": 0.70, "add": 0.70, "replace": 0.55, "swap": 0.15}


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


async def finds(session: AsyncSession, query: str, title_fragment: str) -> bool:
    return any(
        title_fragment in title for title, _tier, _score in await search_rows(session, query)
    )


async def recall(
    session: AsyncSession, kind: str, *, min_length: int = 0
) -> tuple[float, list[str]]:
    """Share of damaged variants that still find their catalog, and the ones that did not."""
    hits, total, missed = 0, 0, []
    for word, fragment in TARGETS:
        if len(word) < min_length:
            continue
        for variant in sorted(damage(word)[kind]):
            total += 1
            if await finds(session, variant, fragment):
                hits += 1
            else:
                missed.append(f"{word}>{variant}")
    return hits / total, missed


async def set_threshold(session: AsyncSession, value: float) -> None:
    # `<%` reads this setting; SET LOCAL ends with the test's transaction.
    await session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {value}"))


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


async def test_very_short_words_are_not_forgiven(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """Pinned limit: `tv5` damaged to `tv`, or `liber` to `libr`, is too little text to compare.
    If this starts passing the threshold was loosened, and the unrelated-word test below should
    be the first thing to look at."""
    rate, _ = await recall(db_session, "replace", min_length=1)
    short = [(w, f) for w, f in TARGETS if len(w) <= 5]
    hits = 0
    total = 0
    for word, fragment in short:
        for variant in sorted(damage(word)["replace"]):
            total += 1
            hits += await finds(db_session, variant, fragment)

    assert hits / total < 0.5, f"{hits}/{total}"
    assert rate >= RECALL_FLOORS["replace"]  # and the long words carry the overall figure


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
    await set_threshold(db_session, 0.3)

    noisy = [w for w in UNRELATED if await search_rows(db_session, w)]

    assert len(noisy) >= 2, noisy


async def test_the_recall_check_can_fail(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """The mirror: tighten the threshold and recall must drop under its floor."""
    await set_threshold(db_session, 0.9)

    rate, _ = await recall(db_session, "drop")

    assert rate < RECALL_FLOORS["drop"], f"{rate:.0%}"


async def test_the_threshold_is_the_postgresql_default(db_session: AsyncSession) -> None:
    """The search statement does not set it, so the server's value decides what counts as a
    typo. A role or database level override would change every result silently; this makes
    that a failing test instead."""
    # The setting only exists once the extension's library is loaded in the session, which its
    # first function call does.
    await db_session.execute(text("SELECT public.word_similarity('a', 'a')"))
    value = (await db_session.execute(text("SHOW pg_trgm.word_similarity_threshold"))).scalar_one()

    assert value == "0.6"


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
    sit below the threshold."""
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
            assert similarity >= 0.6, (typed, title, similarity)
