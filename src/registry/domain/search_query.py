"""Pure parsing of the user's search text (ADR-051, ADR-054, ADR-059).

Splitting only. Nothing here folds, lowercases or strips accents: Python's Unicode
decomposition and PostgreSQL's `unaccent` disagree on `ß`, `Ø`, `Ł`, `æ` and `œ`, so the
database normalises both the stored text and the typed text with the same `fold()`.
"""

import re
from dataclasses import dataclass
from typing import Final

#: The highest page the API serves (ADR-054). `next` is never offered beyond it.
MAX_PAGE: Final = 1000
MAX_QUERY_CHARS: Final = 256
MAX_QUERY_CHUNKS: Final = 16

#: `-"a phrase"`, `"a phrase"`, `-word`, `word`. Groups: 1/2 quoted, 3/4 bare.
_TOKEN = re.compile(r'(-?)"([^"]*)"|(-?)(\S+)')


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    #: Chunks that are OR-ed together. A quoted phrase stays one chunk, quotes included.
    positives: tuple[str, ...]
    #: Chunks that remove catalogs from both halves of the search.
    negatives: tuple[str, ...]
    #: Positives joined with quotes removed. Folded by the database, not here.
    trigram_text: str

    @property
    def is_empty(self) -> bool:
        """No positive chunk means nothing to find: only spaces, punctuation or negations."""
        return not self.positives


def parse_search_query(text: str | None) -> ParsedQuery:
    """Split *text* into positive and negated chunks, websearch style.

    Input is cut at `MAX_QUERY_CHARS` and parsing stops at `MAX_QUERY_CHUNKS` chunks. A bare
    `OR` is dropped, as `websearch_to_tsquery` does, because every search is any-word already.
    """
    # U+0000 cannot be stored in PostgreSQL text, and asyncpg refuses it as a parameter, which
    # would be a 500. It is not a word character, so removing it changes no search; this is
    # cleaning, not the folding ADR-059 forbids.
    raw = (text or "").replace("\x00", "")[:MAX_QUERY_CHARS]
    positives: list[str] = []
    negatives: list[str] = []
    for match in _TOKEN.finditer(raw):
        if match.group(2) is not None:
            if not match.group(2).strip():
                continue
            chunk, negated = f'"{match.group(2)}"', match.group(1) == "-"
        else:
            chunk, negated = match.group(4), match.group(3) == "-"
            if chunk.upper() == "OR":
                continue
        (negatives if negated else positives).append(chunk)
        if len(positives) + len(negatives) >= MAX_QUERY_CHUNKS:
            break
    trigram_text = " ".join(" ".join(c.replace('"', " ") for c in positives).split())
    return ParsedQuery(tuple(positives), tuple(negatives), trigram_text)
