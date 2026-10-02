"""The search parser: pure, so every case here is a plain assertion."""

import random

import pytest

from registry.domain.search_query import (
    MAX_QUERY_CHARS,
    MAX_QUERY_CHUNKS,
    parse_search_query,
)


def test_words_become_positive_chunks_in_order() -> None:
    parsed = parse_search_query("standard ebooks")

    assert parsed.positives == ("standard", "ebooks")
    assert parsed.negatives == ()
    assert parsed.trigram_text == "standard ebooks"


def test_a_quoted_phrase_stays_one_chunk_and_the_trigram_text_drops_the_quotes() -> None:
    parsed = parse_search_query('"numérique de paris" wallis')

    assert parsed.positives == ('"numérique de paris"', "wallis")
    assert parsed.trigram_text == "numérique de paris wallis"


def test_a_leading_dash_negates_a_word_or_a_phrase() -> None:
    parsed = parse_search_query('bibliotheque -paris -"de bruxelles"')

    assert parsed.positives == ("bibliotheque",)
    assert parsed.negatives == ("paris", '"de bruxelles"')
    assert parsed.trigram_text == "bibliotheque"


@pytest.mark.parametrize("text", [None, "", "   ", "-paris", '""', '"  "', "OR", "or OR"])
def test_input_without_a_positive_chunk_is_empty(text: str | None) -> None:
    assert parse_search_query(text).is_empty


def test_punctuation_alone_is_split_not_judged() -> None:
    # The database drops chunks with no lexeme (`registry_query`); the parser only splits.
    assert parse_search_query("!!! ???").positives == ("!!!", "???")


def test_a_bare_or_is_dropped_in_any_case() -> None:
    assert parse_search_query("paris or wallis OR bruxelles").positives == (
        "paris",
        "wallis",
        "bruxelles",
    )


def test_a_word_that_merely_contains_or_is_kept() -> None:
    assert parse_search_query("orléans").positives == ("orléans",)


def test_nothing_is_folded_or_lowercased() -> None:
    parsed = parse_search_query("Suiße ÉCOLE")

    assert parsed.positives == ("Suiße", "ÉCOLE")
    assert parsed.trigram_text == "Suiße ÉCOLE"


def test_input_is_cut_at_the_character_limit() -> None:
    parsed = parse_search_query("a" * (MAX_QUERY_CHARS + 100))

    assert parsed.positives == ("a" * MAX_QUERY_CHARS,)


def test_parsing_stops_at_the_chunk_limit() -> None:
    parsed = parse_search_query(" ".join(f"w{i}" for i in range(100)))

    assert len(parsed.positives) == MAX_QUERY_CHUNKS


def test_negated_chunks_count_toward_the_chunk_limit() -> None:
    parsed = parse_search_query(" ".join(f"-n{i}" for i in range(100)) + " late")

    assert len(parsed.negatives) == MAX_QUERY_CHUNKS
    assert parsed.is_empty


# Property-style cases. A seeded `random` loop rather than hypothesis, which is not a dependency:
# the same inputs on every run, so a failure is reproducible from its seed and iteration.

_ALPHABET_RANGES = [
    (0x20, 0x7E),  # ASCII, including every query-syntax character
    (0xA0, 0x24F),  # Latin with accents
    (0x370, 0x3FF),  # Greek
    (0x400, 0x4FF),  # Cyrillic
    (0x4E00, 0x4E80),  # CJK
    (0x1F300, 0x1F3FF),  # emoji
    (0x300, 0x36F),  # combining marks
    (0x2000, 0x206F),  # general punctuation, zero-width characters
    (0xD800, 0xDFFF),  # lone surrogates, legal in a Python str
]
_SYNTAX = ['"', "-", " ", "\t", "\n", "OR", "or", "&", "|", "!", ":", "'", "\\"]


def _random_text(rng: random.Random, length: int) -> str:
    pieces: list[str] = []
    size = 0
    while size < length:
        if rng.random() < 0.25:
            piece = rng.choice(_SYNTAX)
        else:
            low, high = rng.choice(_ALPHABET_RANGES)
            piece = chr(rng.randint(low, high))
        pieces.append(piece)
        size += len(piece)
    return "".join(pieces)[:length]


def test_parsing_never_raises_and_always_respects_its_limits_for_random_unicode() -> None:
    rng = random.Random(20261002)

    for iteration in range(3000):
        text = _random_text(rng, rng.randint(0, 1000))
        parsed = parse_search_query(text)

        context = f"iteration {iteration}: {text!r}"
        chunks = parsed.positives + parsed.negatives
        assert len(chunks) <= MAX_QUERY_CHUNKS, context
        assert all(chunk.strip() for chunk in chunks), context
        assert all(len(chunk) <= MAX_QUERY_CHARS + 2 for chunk in chunks), context
        assert not any(chunk.upper() == "OR" for chunk in parsed.positives), context
        assert '"' not in parsed.trigram_text, context
        assert parsed.is_empty == (not parsed.positives), context
        assert parse_search_query(text) == parsed, context  # deterministic


def test_text_beyond_the_character_cap_never_changes_the_result() -> None:
    rng = random.Random(7)

    for _ in range(200):
        text = _random_text(rng, rng.randint(MAX_QUERY_CHARS, 1000))

        assert parse_search_query(text) == parse_search_query(text[:MAX_QUERY_CHARS])


def test_a_chunk_is_never_split_across_the_cap_into_an_extra_chunk() -> None:
    parsed = parse_search_query("a" * 300 + " b")

    assert parsed.positives == ("a" * MAX_QUERY_CHARS,)


def test_an_unbalanced_quote_stays_on_its_word_and_leaves_the_trigram_text() -> None:
    """No closing quote, so no phrase: the quote is kept on the chunk, where
    `websearch_to_tsquery` ignores it, and the trigram text never contains one."""
    parsed = parse_search_query('x "word other')

    assert parsed.positives == ("x", '"word', "other")
    assert parsed.trigram_text == "x word other"


def test_a_nul_byte_is_removed_because_postgresql_text_cannot_hold_it() -> None:
    assert parse_search_query("pa\x00ris").positives == ("paris",)
    assert parse_search_query("\x00").is_empty
    assert parse_search_query("\x00\x00 \x00").is_empty


def test_the_trigram_text_is_the_positives_with_single_spaces() -> None:
    parsed = parse_search_query('  one   "two  three"    four  -five ')

    assert parsed.trigram_text == "one two three four"


def test_mixed_case_or_is_dropped_but_only_as_a_whole_word() -> None:
    parsed = parse_search_query("Or oR OR or ORs")

    assert parsed.positives == ("ORs",)


def test_a_negated_bare_or_is_dropped_like_any_bare_or() -> None:
    """`-or` is not a negation of the word "or": websearch syntax treats OR as an operator."""
    assert parse_search_query("paris -or").negatives == ()
