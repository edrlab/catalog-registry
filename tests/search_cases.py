"""The search table of the test plan (section 1) as plain data.

One `Case` per search: what it checks, the ideal answer ("Expected"), the measured answer
("Today"), and a note. `today` is `(title, tier)` in rank order, measured with the real migrations
and the two data files. Tier 1 is a word match, tier 2 a trigram-only match, which always follows
every word match (ADR-051). Where the plan calls a result known and accepted, the case says so and
`today` pins the CURRENT behaviour, so a change is noticed and has to be decided rather than
slipping in.

Three things read this one file:
* the exact-result regression tests (`CASES`: query and `today`),
* the score (`tests/search_quality.py`, `scripts/score_search.py`): `today` against `expected`,
* the page for Hadrien, generated into `docs/search-test-cases.md` (`make search-cases`).
"""

from dataclasses import dataclass

PARIS = "Bibliothèque numérique de Paris"
LIRTUEL = "Lirtuel"
OPENBARE = "De Openbare bibliotheek Vlaanderen en Brussel"
VALAIS = "Médiathèque Valais"
ROMANDE = "Bibliothèque numérique Romande"
RUSSE = "La Bibliothèque russe et slave"
GUTENBERG = "Project Gutenberg"
LIBRIVOX = "Librivox"
STANDARD = "Standard Ebooks"
LIBRES = "Ebooks libres et gratuits"
TV5 = "TV5 Monde"
LIBER = "Liber Liber"


def words(*titles: str) -> list[tuple[str, int]]:
    return [(title, 1) for title in titles]


def trigrams(*titles: str) -> list[tuple[str, int]]:
    return [(title, 2) for title in titles]


@dataclass(frozen=True)
class Case:
    """One search of the test plan: what we ask, what we would like back, and what we get today.

    `expected` is the ideal answer, the "Expected" column of the plan: the catalogs a reader
    should get, with `first` pinned to rank 1 when that matters.
    `None` means the plan gives no judgement (only "no error"), so the case is not scored. An
    empty tuple means "nothing". `today` is the measured result, `(title, tier)` in rank order,
    which the regression tests pin exactly; the gap between the two is what the score measures.
    """

    section: str
    query: str
    checks: str
    expected: tuple[str, ...] | None
    today: list[tuple[str, int]]
    note: str = ""
    first: str | None = None


#: Short for the table below, which has sixty rows.
c = Case


PLACES = "Place names in several languages"
NOT_LOADED = "Languages we don't load for that country"
TITLE = "Titles"
TYPO = "Typos, plurals, partial words"
SYNTAX_SECTION = "Query syntax"
COMMON = "Short common words (no stop words yet)"
NO_COUNTRY = "Catalogs without a country"

#: Every scored search of the plan, in the order of the plan. Ids are not spelled out: titles are
#: unique in the data.
QUALITY_CASES: list[Case] = [
    c(
        PLACES,
        "Paris",
        "Title word, and since the city was filled in also the city",
        (PARIS,),
        words(PARIS),
        "The city (`Paris`, label C) adds to the title match: the score went from 0.608 to 0.638.",
    ),
    c(PLACES, "ile-de-france", "Region name with hyphens", (PARIS,), words(PARIS)),
    c(
        PLACES,
        "Île de France",
        "Same name typed without hyphens",
        (PARIS,),
        words(PARIS, OPENBARE, LIRTUEL),
        "Both Belgian libraries come back because of 'de' (Région de Bruxelles-Capitale, "
        "De Openbare). Accepted for now (Q5): there are no stop words.",
        first=PARIS,
    ),
    c(PLACES, "France", "Country name, weight B", (PARIS,), words(PARIS)),
    c(PLACES, "Belgique", "Country, French", (LIRTUEL, OPENBARE), words(LIRTUEL, OPENBARE)),
    c(PLACES, "Belgium", "Country, English", (LIRTUEL, OPENBARE), words(LIRTUEL, OPENBARE)),
    c(
        PLACES,
        "België",
        "Country, Dutch, accent folded",
        (LIRTUEL, OPENBARE),
        words(LIRTUEL, OPENBARE),
    ),
    c(PLACES, "Belgien", "Country, German", (LIRTUEL, OPENBARE), words(LIRTUEL, OPENBARE)),
    c(PLACES, "Bruxelles", "Region, French", (LIRTUEL, OPENBARE), words(LIRTUEL, OPENBARE)),
    c(PLACES, "Brussels", "Region, English", (LIRTUEL, OPENBARE), words(LIRTUEL, OPENBARE)),
    c(
        PLACES,
        "Brussel",
        "Region, Dutch, also in a title",
        (OPENBARE, LIRTUEL),
        words(OPENBARE, LIRTUEL),
        first=OPENBARE,
    ),
    c(
        PLACES,
        "Brüssel",
        "Region, German",
        (OPENBARE, LIRTUEL),
        words(OPENBARE, LIRTUEL),
        first=OPENBARE,
    ),
    c(PLACES, "Wallonie", "Region, French", (LIRTUEL,), words(LIRTUEL)),
    c(PLACES, "Wallonia", "Region, English", (LIRTUEL,), words(LIRTUEL)),
    c(
        PLACES,
        "Vlaanderen",
        "Region, Dutch everyday name, also in the title",
        (OPENBARE,),
        words(OPENBARE),
    ),
    c(
        PLACES,
        "Flandre",
        "Region, French everyday name added by hand",
        (OPENBARE,),
        words(OPENBARE),
    ),
    c(PLACES, "Flanders", "Region, English", (OPENBARE,), words(OPENBARE)),
    c(PLACES, "Valais", "Canton, French and English, also in the title", (VALAIS,), words(VALAIS)),
    c(
        PLACES,
        "Wallis",
        "Canton, German",
        (VALAIS,),
        words(VALAIS) + trigrams(LIRTUEL),
        "Lirtuel follows as a trigram match (Wallonia is close to Wallis) since the typo "
        "threshold moved to 0.5.",
    ),
    c(PLACES, "Vallese", "Canton, Italian", (VALAIS,), words(VALAIS)),
    c(PLACES, "Suisse", "Country, French", (VALAIS,), words(VALAIS)),
    c(PLACES, "Schweiz", "Country, German", (VALAIS,), words(VALAIS)),
    c(PLACES, "Svizzera", "Country, Italian", (VALAIS,), words(VALAIS)),
    c(
        PLACES,
        "Bristol",
        "Place with no catalog, close to Brussel",
        (),
        [],
        "Trigrams don't pull in the Belgian libraries.",
    ),
    c(
        PLACES,
        "région",
        "Word found in Belgian region names",
        (LIRTUEL, OPENBARE),
        words(OPENBARE, LIRTUEL),
    ),
    c(PLACES, "canton", "Word found in Swiss canton names", (VALAIS,), words(VALAIS)),
    c(
        NOT_LOADED,
        "Belgio",
        "Italian name of Belgium, Italian isn't official there",
        (),
        trigrams(LIRTUEL, OPENBARE),
        "Found through trigrams, close to Belgie and Belgien. Harmless, arguably helpful.",
    ),
    c(
        NOT_LOADED,
        "Vallonia",
        "Italian name of Wallonia",
        (),
        trigrams(LIRTUEL),
        "Found through trigrams, close to Wallonia. Same as above.",
    ),
    c(NOT_LOADED, "Zwitserland", "Dutch name of Switzerland, Dutch isn't official there", (), []),
    c(TITLE, "Gutenberg", "Title word", (GUTENBERG,), words(GUTENBERG)),
    c(
        TITLE,
        "bibliotheque",
        "Title word without accents",
        (PARIS, ROMANDE, RUSSE),
        words(PARIS, ROMANDE, RUSSE) + trigrams(OPENBARE),
        "De Openbare bibliotheek also comes back through trigrams, after the three exact matches.",
    ),
    c(TITLE, "MÉDIATHÈQUE", "Upper case with accents", (VALAIS,), words(VALAIS)),
    c(TITLE, "ebooks", "Shared title word", (STANDARD, LIBRES), words(STANDARD, LIBRES)),
    c(
        TITLE,
        "standard ebooks",
        "Two words, any-word matching",
        (STANDARD, LIBRES),
        words(STANDARD, LIBRES),
        first=STANDARD,
    ),
    c(
        TITLE,
        "Liber",
        "Short title word, close to libres",
        (LIBER,),
        words(LIBER) + trigrams(LIBRIVOX, LIBRES),
        "Librivox and Ebooks libres follow as trigram matches since the typo threshold moved "
        "to 0.5.",
    ),
    c(TITLE, "Librivox", "Title word", (LIBRIVOX,), words(LIBRIVOX)),
    c(
        TITLE,
        "Bibliothèque Nationale",
        "Two words, only one of them in a title",
        (PARIS, ROMANDE, RUSSE),
        words(PARIS, ROMANDE, RUSSE),
        "There's no national library in the seed data, so these match on 'bibliothèque' alone "
        "and have the same score.",
    ),
    c(TITLE, "TV5", "Word with a digit", (TV5,), words(TV5)),
    c(TYPO, "gutenbrg", "Missing letter in a title", (GUTENBERG,), trigrams(GUTENBERG)),
    c(
        TYPO,
        "bruxels",
        "Missing letters in a place name",
        (LIRTUEL, OPENBARE),
        trigrams(LIRTUEL, OPENBARE),
    ),
    c(
        TYPO,
        "belgiqe",
        "Missing letter in a country name",
        (LIRTUEL, OPENBARE),
        trigrams(LIRTUEL, OPENBARE),
    ),
    c(
        TYPO,
        "bibliothèques",
        "Plural",
        (PARIS, ROMANDE, RUSSE),
        trigrams(PARIS, ROMANDE, RUSSE, OPENBARE),
    ),
    c(TYPO, "guten", "Start of a word", (GUTENBERG,), trigrams(GUTENBERG)),
    c(
        TYPO,
        "parsi",
        "Two letters swapped in a short word",
        (PARIS,),
        trigrams(PARIS),
        "Found through trigrams since the typo threshold moved to 0.5 (nothing at 0.6). "
        "Swapped letters stay the weakest typo: about two in three are found.",
    ),
    c(
        TYPO,
        "Suiße",
        "ß is folded by the database's unaccent, not by Python (ADR-059)",
        (VALAIS,),
        words(VALAIS),
    ),
    c(
        SYNTAX_SECTION,
        '"numérique de paris"',
        "Quoted phrase",
        (PARIS,),
        words(PARIS) + trigrams(ROMANDE),
        "Romande follows as a trigram match: the fuzzy half ignores the quotes.",
    ),
    c(
        SYNTAX_SECTION,
        "bibliothèque -paris",
        "Excluding a word",
        (ROMANDE, RUSSE),
        words(ROMANDE, RUSSE) + trigrams(OPENBARE),
        "Negated words exclude catalogs from both halves, so BnParis never comes back.",
    ),
    c(
        SYNTAX_SECTION,
        "Belgique -lirtuel",
        "Negation keeps a real score",
        (OPENBARE,),
        words(OPENBARE),
    ),
    c(
        SYNTAX_SECTION,
        "paris OR valais",
        "OR",
        (PARIS, VALAIS),
        words(VALAIS, PARIS),
    ),
    c(SYNTAX_SECTION, "PARIS or", "A bare OR is dropped", (PARIS,), words(PARIS)),
    c(
        SYNTAX_SECTION,
        '"de paris',
        "Unbalanced quote",
        None,
        words(PARIS, OPENBARE, LIRTUEL),
        "No error is the requirement. Treated as the words 'de' and 'paris'; 'de' noise as above.",
    ),
    c(
        SYNTAX_SECTION,
        "brussel -",
        "Lone dash",
        (OPENBARE, LIRTUEL),
        words(OPENBARE, LIRTUEL),
        "As Brussel, no error.",
        first=OPENBARE,
    ),
    c(SYNTAX_SECTION, "& | ! :", "Special characters only", (), [], "Nothing, and no error."),
    c(SYNTAX_SECTION, "xyzzy", "No match at all", (), []),
    c(SYNTAX_SECTION, "", "Empty search", (), [], "Decided: stays empty, no database work."),
    c(SYNTAX_SECTION, "   ", "Search with only spaces", (), [], "Same as empty."),
    c(SYNTAX_SECTION, "-paris", "Only a negation", (), [], "Same as empty."),
    c(
        COMMON,
        "bibliothèque de Paris",
        "Three words, one of them 'de'",
        (PARIS,),
        words(PARIS, OPENBARE, ROMANDE, RUSSE, LIRTUEL),
        "De Openbare comes second because of 'de' in its title. The stop word problem (Q5).",
        first=PARIS,
    ),
]

#: A trailing section of the plan: countries of catalogs that have none, which no search finds.
QUALITY_CASES += [
    c(
        NO_COUNTRY,
        "Italy",
        "Country of a catalog with no country set (Liber Liber)",
        (),
        [],
        "Correct for the data we have.",
    ),
    c(NO_COUNTRY, "Italia", "Same, in Italian", (), []),
    c(NO_COUNTRY, "Italie", "Same, in French", (), []),
    c(NO_COUNTRY, "United States", "Same, for Project Gutenberg and Standard Ebooks", (), []),
]

REALISTIC = "Realistic typos, one slip per word"
NOT_FOUND = "Not found: swapped or replaced letters in a short word share too few trigrams."

#: What readers actually mistype: a letter dropped, doubled, replaced by the neighbouring key, or
#: two neighbours swapped. Measured at the 0.5 threshold; the 0.6 results are in docs/search.md.
REALISTIC_TYPOS: list[Case] = [
    c(
        REALISTIC,
        "belgque",
        "One letter dropped in a country name",
        (LIRTUEL, OPENBARE),
        trigrams(LIRTUEL, OPENBARE),
    ),
    c(
        REALISTIC,
        "bruxeles",
        "One letter dropped in a place name",
        (LIRTUEL, OPENBARE),
        trigrams(LIRTUEL, OPENBARE),
    ),
    c(
        REALISTIC,
        "libriox",
        "One letter dropped in a title",
        (LIBRIVOX,),
        trigrams(LIBRIVOX, LIBRES),
    ),
    c(REALISTIC, "valas", "One letter dropped in a canton", (VALAIS,), trigrams(VALAIS)),
    c(REALISTIC, "walonie", "One letter dropped in a region", (LIRTUEL,), trigrams(LIRTUEL)),
    c(REALISTIC, "flandrs", "One letter dropped in a region", (OPENBARE,), trigrams(OPENBARE)),
    c(REALISTIC, "suise", "One letter dropped in a country name", (VALAIS,), trigrams(VALAIS)),
    c(REALISTIC, "standrd", "One letter dropped in a title word", (STANDARD,), trigrams(STANDARD)),
    c(
        REALISTIC,
        "pairs",
        "Two neighbouring letters swapped in a title word",
        (PARIS,),
        [],
        note=NOT_FOUND,
    ),
    c(
        REALISTIC,
        "valias",
        "Two neighbouring letters swapped in a canton",
        (VALAIS,),
        [],
        note=NOT_FOUND,
    ),
    c(
        REALISTIC,
        "brussles",
        "Two neighbouring letters swapped in a place name",
        (LIRTUEL, OPENBARE),
        trigrams(LIRTUEL, OPENBARE),
    ),
    c(
        REALISTIC,
        "gutneberg",
        "Two neighbouring letters swapped in a title",
        (GUTENBERG,),
        [],
        note=NOT_FOUND,
    ),
    c(
        REALISTIC,
        "belguim",
        "Two neighbouring letters swapped in a country name",
        (LIRTUEL, OPENBARE),
        trigrams(LIRTUEL, OPENBARE),
    ),
    c(
        REALISTIC,
        "wallonei",
        "Two neighbouring letters swapped in a region",
        (LIRTUEL,),
        trigrams(LIRTUEL),
    ),
    c(
        REALISTIC,
        "schwiez",
        "Two neighbouring letters swapped in a country name",
        (VALAIS,),
        trigrams(VALAIS),
    ),
    c(
        REALISTIC,
        "bibliotehque",
        "Two neighbouring letters swapped in a title word",
        (PARIS, ROMANDE, RUSSE),
        trigrams(PARIS, OPENBARE, ROMANDE, RUSSE),
    ),
    c(
        REALISTIC,
        "parus",
        "One letter replaced by a neighbouring key in a title word",
        (PARIS,),
        trigrams(PARIS),
    ),
    c(
        REALISTIC,
        "belgiqie",
        "One letter replaced by a neighbouring key in a country name",
        (LIRTUEL, OPENBARE),
        trigrams(LIRTUEL, OPENBARE),
    ),
    c(
        REALISTIC,
        "vqlais",
        "One letter replaced by a neighbouring key in a canton",
        (VALAIS,),
        [],
        note=NOT_FOUND,
    ),
    c(
        REALISTIC,
        "frabce",
        "One letter replaced by a neighbouring key in a country name",
        (PARIS,),
        trigrams(PARIS),
    ),
    c(
        REALISTIC,
        "lirtuek",
        "One letter replaced by a neighbouring key in a title",
        (LIRTUEL,),
        trigrams(LIRTUEL),
    ),
    c(
        REALISTIC,
        "bibliotheuqe",
        "One letter replaced by a neighbouring key in a title word",
        (PARIS, ROMANDE, RUSSE),
        trigrams(PARIS, OPENBARE, ROMANDE, RUSSE),
    ),
    c(
        REALISTIC,
        "guttenberg",
        "One letter doubled or added in a title",
        (GUTENBERG,),
        trigrams(GUTENBERG),
    ),
    c(
        REALISTIC,
        "belgiumm",
        "One letter doubled or added in a country name",
        (LIRTUEL, OPENBARE),
        trigrams(LIRTUEL, OPENBARE),
    ),
    c(
        REALISTIC,
        "bruxellles",
        "One letter doubled or added in a place name",
        (LIRTUEL, OPENBARE),
        trigrams(LIRTUEL, OPENBARE),
    ),
    c(
        REALISTIC,
        "pariss",
        "One letter doubled or added in a title word",
        (PARIS,),
        trigrams(PARIS),
    ),
    c(
        REALISTIC,
        "librivoxx",
        "One letter doubled or added in a title",
        (LIBRIVOX,),
        trigrams(LIBRIVOX),
    ),
    c(
        REALISTIC,
        "standrad ebooks",
        "Two words, a swap in one of them",
        (STANDARD, LIBRES),
        words(STANDARD, LIBRES),
        first=STANDARD,
    ),
    c(
        REALISTIC,
        "belgqiue",
        "Two slips in one word in a country name",
        (LIRTUEL, OPENBARE),
        trigrams(LIRTUEL, OPENBARE),
    ),
]

#: The realistic typos sit right after the hand-picked ones, before the query syntax.
_AT = next(i for i, case in enumerate(QUALITY_CASES) if case.section == SYNTAX_SECTION)
QUALITY_CASES[_AT:_AT] = REALISTIC_TYPOS


@dataclass(frozen=True)
class PageCase:
    """A search walked page by page. The real page size is 50, which twelve catalogs cannot fill,
    so the same query is read with a smaller page: the pages must add up to the whole list with
    nothing repeated or skipped, in the same order every time."""

    query: str
    size: int
    pages: tuple[tuple[str, ...], ...]
    checks: str
    note: str = ""


PAGE_CASES: list[PageCase] = [
    PageCase(
        "bibliothèque de Paris",
        2,
        ((PARIS, OPENBARE), (ROMANDE, RUSSE), (LIRTUEL,)),
        "Pages add up to the full list, nothing repeated or skipped",
    ),
    PageCase(
        "Belgique",
        1,
        ((LIRTUEL,), (OPENBARE,)),
        "Two catalogs with the same score keep the same order, run after run",
        "Both have exactly the same score. The order comes from the tie-breaker (import date, then "
        "id), which the query has to keep.",
    ),
]

#: The exact-result regression tests read these. (query, [(title, tier)])
CASES = [(case.query, case.today) for case in QUALITY_CASES]
