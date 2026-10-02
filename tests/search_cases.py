"""The search table of the test plan (section 1) as plain data: (query, [(title, tier)]).

Expected results are in rank order, measured on 1 October with the real migrations and the two
data files. Tier 1 is a word match, tier 2 a trigram-only match, which always follows every word
match (ADR-051). Where the plan calls a result known and accepted, the case says so and pins the
CURRENT behaviour, so a change is noticed and has to be decided rather than slipping in.

Kept as data so plan section 6 (a search-quality score) can become a small step.
"""

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


#: (query, expected [(title, tier)]). Ids are not spelled out: titles are unique in the data.
PLACE_NAMES = [
    ("Paris", words(PARIS)),
    ("ile-de-france", words(PARIS)),
    # Accepted (Q5): 'de' is not a stop word, so both Belgian libraries come in below BnParis.
    ("Île de France", words(PARIS, OPENBARE, LIRTUEL)),
    ("France", words(PARIS)),
    ("Belgique", words(LIRTUEL, OPENBARE)),
    ("Belgium", words(LIRTUEL, OPENBARE)),
    ("België", words(LIRTUEL, OPENBARE)),
    ("Belgien", words(LIRTUEL, OPENBARE)),
    ("Bruxelles", words(LIRTUEL, OPENBARE)),
    ("Brussels", words(LIRTUEL, OPENBARE)),
    ("Brussel", words(OPENBARE, LIRTUEL)),
    ("Brüssel", words(OPENBARE, LIRTUEL)),
    ("Wallonie", words(LIRTUEL)),
    ("Wallonia", words(LIRTUEL)),
    ("Vlaanderen", words(OPENBARE)),
    ("Flandre", words(OPENBARE)),
    ("Flanders", words(OPENBARE)),
    ("Valais", words(VALAIS)),
    ("Wallis", words(VALAIS)),
    ("Vallese", words(VALAIS)),
    ("Suisse", words(VALAIS)),
    ("Schweiz", words(VALAIS)),
    ("Svizzera", words(VALAIS)),
    ("région", words(OPENBARE, LIRTUEL)),
    ("canton", words(VALAIS)),
]

NOT_LOADED_LANGUAGES = [
    # Accepted: Italian is not loaded for Belgium, but trigrams are close to Belgie/Belgien.
    ("Belgio", trigrams(LIRTUEL, OPENBARE)),
    # Accepted: close to Wallonia.
    ("Vallonia", trigrams(LIRTUEL)),
    ("Zwitserland", []),
]

TITLES = [
    ("Gutenberg", words(GUTENBERG)),
    # Accepted: 'bibliotheek' in De Openbare's title comes back as a trigram match, after the
    # three exact ones.
    ("bibliotheque", words(PARIS, ROMANDE, RUSSE) + trigrams(OPENBARE)),
    ("MÉDIATHÈQUE", words(VALAIS)),
    ("ebooks", words(STANDARD, LIBRES)),
    ("standard ebooks", words(STANDARD, LIBRES)),
    ("Liber", words(LIBER)),
    ("Librivox", words(LIBRIVOX)),
    ("TV5", words(TV5)),
    ("Bibliothèque Nationale", words(PARIS, ROMANDE, RUSSE)),
    ("Bristol", []),
]

TYPOS = [
    ("gutenbrg", trigrams(GUTENBERG)),
    ("bruxels", trigrams(LIRTUEL, OPENBARE)),
    ("belgiqe", trigrams(LIRTUEL, OPENBARE)),
    ("bibliothèques", trigrams(PARIS, ROMANDE, RUSSE, OPENBARE)),
    ("guten", trigrams(GUTENBERG)),
    # Known trigram limit: swapped letters in a short word share too few trigrams.
    ("parsi", []),
    # ß is folded by the database's unaccent, not by Python (ADR-059).
    ("Suiße", words(VALAIS)),
]

SYNTAX = [
    ('"numérique de paris"', words(PARIS)),
    # Negation removes from both halves: BnParis used to come back through the trigram half.
    ("bibliothèque -paris", words(ROMANDE, RUSSE) + trigrams(OPENBARE)),
    ("Belgique -lirtuel", words(OPENBARE)),
    ("paris OR valais", words(VALAIS, PARIS)),
    ("PARIS or", words(PARIS)),
    # Accepted (Q5): treated as the words 'de' and 'paris'.
    ('"de paris', words(PARIS, OPENBARE, LIRTUEL)),
    ("brussel -", words(OPENBARE, LIRTUEL)),
    ("& | ! :", []),
    ("xyzzy", []),
    ("", []),
    ("   ", []),
    ("-paris", []),
]

SHORT_COMMON_WORDS = [
    # Accepted (Q5): no stop words, so 'de' pulls in the Belgian libraries and the other
    # Bibliothèque catalogs, all below BnParis.
    ("bibliothèque de Paris", words(PARIS, OPENBARE, ROMANDE, RUSSE, LIRTUEL)),
]

CASES = PLACE_NAMES + NOT_LOADED_LANGUAGES + TITLES + TYPOS + SYNTAX + SHORT_COMMON_WORDS
