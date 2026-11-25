"""What the typo allowance can and cannot find, measured instead of assumed.

The hand-picked typo rows in the search table prove a handful of cases work. They say nothing
about *how far* the fuzziness reaches, so these tests derive it from first principles: take every
word the data is findable by, damage it in each of the four ways a person mistypes (drop a letter,
add one, replace one, swap two neighbours), and count how often the right catalog still comes back.
The result is an envelope with floors under it, plus the converse: words unrelated to anything in
the data must return nothing.

Why the numbers are what they are: `registry_tinql` lets a word of 6 letters or more be 2 edits
away (`word~2`), a word of 4 or 5 letters 1 edit (`~1`), a shorter one none. Drop, add and replace
are one edit each, a swap of neighbours two. TIN keeps the first letter fixed. The last word of a
query also matches as a prefix. When TIN finds nothing at all, a trigram match on words of 4
letters or more rescues the search (`pg_trgm` word similarity at 0.5): a typo in the first letter,
a swap in a short word.

Measured on the 12-catalog seed, on Lead (TIN's open-source stand-in):

    edit       trigrams at 0.5 (2026-10)   TIN alone   TIN + trigram rescue
    drop       96%                         87%         100%
    add        96%                         89%          99%
    replace    90%                         87%          97%
    swap       66%                         81%          86%
    first letter (drop/replace, of 46)     -            0          40
    unrelated words returned (of 40):  1 (library)  0           0
"""

import collections
import unicodedata

import pytest
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

#: Ordinary words with no connection to any catalog. None may bring anything back; a word that
#: does is noise a reader would see.
UNRELATED = [
    "bristol", "kitchen", "qwerty", "telephone", "zzzzqqq", "computer", "amsterdam", "london",
    "tokyo", "guitar", "orange", "bridge", "castle", "mountain", "river", "music", "history",
    "science", "poetry", "novel", "comic", "garden", "window", "yellow", "silver", "market",
    "bakery", "cinema", "museum", "harbour", "coffee", "winter", "summer", "school",
    "holland", "station", "hospital", "pasta", "zwitserland", "kestrel",
]  # fmt: skip

#: Under the measured recall (see the module docstring), so a real regression fails and ordinary
#: variation in the data does not.
RECALL_FLOORS = {"drop": 0.97, "add": 0.96, "replace": 0.94, "swap": 0.83}


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
    rows = await search_rows(session, query)
    return any(title_fragment in title for title, _tier, _score in rows)


async def recall(
    session: AsyncSession,
    kind: str,
    *,
    min_length: int = 0,
    max_length: int = 99,
    first_letter: bool = True,
) -> tuple[float, list[str]]:
    """Share of damaged variants that still find their catalog, and the ones that did not."""
    hits, total, missed = 0, 0, []
    for word, fragment in TARGETS:
        if not min_length <= len(word) <= max_length:
            continue
        for variant in sorted(damage(word)[kind]):
            if not first_letter and variant[0] != word[0]:
                continue
            total += 1
            if await finds(session, variant, fragment):
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


async def test_one_edit_after_the_first_letter_of_a_long_word_is_always_found(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """A guarantee, not a rate: a word of 6 letters or more is allowed 2 edits, so dropping,
    adding or replacing any letter but the first, or swapping two neighbours after it, still
    finds it."""
    for kind in ("drop", "add", "replace", "swap"):
        rate, missed = await recall(db_session, kind, min_length=6, first_letter=False)

        assert missed == [], (kind, missed)
        assert rate == 1.0


async def test_damage_to_the_first_letter_is_forgiven_by_the_trigram_rescue(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """TIN keeps the first letter fixed (`~N` means `~1:N`), so on its own `xutenberg` finds
    nothing; the trigram rescue does, ranked below any TIN hit (a score under 0)."""
    rows = await search_rows(db_session, "xutenberg")

    assert [title for title, _tier, _score in rows] == ["Project Gutenberg"]
    assert all(score <= 0 for _t, _tier, score in rows)
    first_total = first_found = 0
    for word, fragment in TARGETS:
        for kind in ("drop", "replace"):
            for variant in damage(word)[kind]:
                if variant[0] != word[0]:
                    first_total += 1
                    first_found += await finds(db_session, variant, fragment)
    assert first_found / first_total >= 0.85, f"{first_found}/{first_total}"


async def test_two_letters_swapped_in_a_five_letter_word_stay_a_known_limit(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """`pairs` is two TIN edits from `paris` (a 5-letter word is allowed one), and the swap breaks
    four of its six three-letter pieces (word similarity about 0.33, under 0.5). Pinned so a change
    that finds it is noticed and measured; longer words swapped are found."""
    assert not await finds(db_session, "pairs", "Paris")
    assert await finds(db_session, "bilbiotheque", "Paris")


async def test_the_rescue_never_adds_to_a_search_tin_answered(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """A rescue, not a second opinion: when TIN finds anything, no trigram match is added, so a
    correct search keeps exactly its answer (no score at or under 0 unless TIN scored it so)."""
    for query in ("gutenberg", "paris", "bibliotheque", "belgique", "standard ebooks"):
        rows = await search_rows(db_session, query)
        assert rows, query
        assert all(score > 0 for _t, _tier, score in rows), (query, rows)


async def test_a_quoted_phrase_is_never_loosened_into_words(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """A phrase TIN cannot find returns nothing, not the catalogs one of its words resembles."""
    assert await search_rows(db_session, '"xutenberg project"') == []
    assert await finds(db_session, "xutenberg project", "Gutenberg")


async def test_words_under_four_letters_are_never_rescued(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """Three letters share too few three-letter pieces to tell a typo from another word."""
    assert await search_rows(db_session, "prs") == []
    assert await search_rows(db_session, "xib") == []


async def test_an_exclusion_also_removes_rescued_catalogs(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    assert await finds(db_session, "xutenberg", "Gutenberg")
    assert not await finds(db_session, "xutenberg -gutenberg", "Gutenberg")


# --- precision: what the fuzziness must not drag in --------------------------------------------


@pytest.mark.parametrize("word", UNRELATED)
async def test_a_word_unrelated_to_anything_in_the_data_returns_nothing(
    db_session: AsyncSession, searchable_catalogs: None, word: str
) -> None:
    assert await search_rows(db_session, word) == []


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
    """Folding happens in the database for typed and stored text alike."""
    folded = unicodedata.normalize("NFD", typed).encode("ascii", "ignore").decode().lower()
    assert folded == same_as, "the pair must be the same word once folded"

    assert await search_rows(db_session, typed) == await search_rows(db_session, same_as)


async def test_a_typo_only_match_scores_zero_and_an_exact_one_more(
    db_session: AsyncSession, searchable_catalogs: None
) -> None:
    """On Lead, TIN scores a fuzzy or prefix term by the word as typed, so a catalog found only
    through the typo allowance scores 0 (production TIN scores it)."""
    assert {score for _t, _tier, score in await search_rows(db_session, "gutenbrg")} == {0.0}
    assert all(score > 0 for _t, _tier, score in await search_rows(db_session, "gutenberg"))
