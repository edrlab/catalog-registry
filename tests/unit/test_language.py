"""Parsing an `Accept-Language` header, and matching it against a catalog's tags."""

import pytest

from registry.domain.language import (
    MAX_LANGUAGE_RANGES,
    is_wildcard_range,
    match_depth,
    match_language_ranges,
    normalise_language_tag,
    parse_accept_language,
    rank_language_match,
)
from registry.domain.models import LanguageMatch, LanguageRange

pytestmark = pytest.mark.unit


def tags(header: str | None) -> list[str]:
    return [range_.tag for range_ in parse_accept_language(header)]


# --- §5.1 parsing ---------------------------------------------------------------------


@pytest.mark.parametrize("header", [None, "", "   ", ";;;,,,"])
def test_parse_accept_language_unusable_header_returns_no_ranges(header: str | None) -> None:
    assert parse_accept_language(header) == ()


def test_parse_accept_language_single_tag_defaults_to_quality_one() -> None:
    assert parse_accept_language("fr") == (LanguageRange(tag="fr", quality=1.0),)


def test_parse_accept_language_wildcard_is_a_range() -> None:
    (range_,) = parse_accept_language("*")

    assert is_wildcard_range(range_)


def test_parse_accept_language_region_is_preserved() -> None:
    assert tags("fr-BE") == ["fr-be"]


def test_parse_accept_language_orders_by_quality_descending() -> None:
    assert tags("en;q=0.8, fr;q=0.9") == ["fr", "en"]


def test_parse_accept_language_equal_quality_preserves_header_order() -> None:
    assert tags("fr, de") == ["fr", "de"]


def test_parse_accept_language_keeps_a_zero_quality_range() -> None:
    """`q=0` is "not acceptable", which is a statement. Dropping it at parse time would make
    `fr;q=0` indistinguishable from no header at all, and the client would be served French."""
    assert [(r.tag, r.quality) for r in parse_accept_language("fr;q=0")] == [("fr", 0.0)]
    assert tags("fr;q=0, en") == ["en", "fr"]


def test_parse_accept_language_zero_quality_range_never_matches() -> None:
    assert match_language_ranges(parse_accept_language("fr;q=0"), ["fr"]) == ()


def test_parse_accept_language_quality_above_one_is_clamped() -> None:
    assert parse_accept_language("fr;q=1.5")[0].quality == 1.0


def test_parse_accept_language_malformed_quality_is_treated_as_one() -> None:
    assert parse_accept_language("fr;q=abc")[0].quality == 1.0


def test_parse_accept_language_quality_is_truncated_to_three_decimals() -> None:
    assert parse_accept_language("fr;q=0.1234")[0].quality == 0.123


def test_parse_accept_language_lowercases_the_tag() -> None:
    assert tags("FR-be") == ["fr-be"]


def test_parse_accept_language_bounds_the_number_of_ranges() -> None:
    header = ",".join(f"x{index}" for index in range(500))

    assert len(parse_accept_language(header)) == MAX_LANGUAGE_RANGES


def test_parse_accept_language_keeps_three_subtags_intact() -> None:
    assert tags("zh-Hans-CN") == ["zh-hans-cn"]


def test_parse_accept_language_preserves_a_singleton_subtag() -> None:
    assert tags("de-a-value") == ["de-a-value"]


def test_normalise_language_tag_is_idempotent() -> None:
    once = normalise_language_tag(" FR_be ")

    assert once == "fr-be"
    assert normalise_language_tag(once) == once


# --- §5.2 matching --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "available", "expected"),
    [
        ("fr", ["fr"], ("fr",)),
        ("fr", ["fr-be"], ("fr-be",)),  # Filtering, the range widens to `fr-be`
        ("fr-be", ["fr"], ("fr",)),  # Lookup truncation, the range narrows to `fr`
        ("fr", ["frr"], ()),
        ("*", ["fr", "de"], ("fr", "de")),
        ("de-a-value", ["de"], ("de",)),
        ("fr", [], ()),
        ("fr, en", ["en"], ("en",)),
        ("FR", ["fr"], ("fr",)),
        ("fr", ["de"], ()),
    ],
)
def test_match_language_ranges(
    header: str, available: list[str], expected: tuple[str, ...]
) -> None:
    assert match_language_ranges(parse_accept_language(header), available) == expected


def test_match_language_ranges_returns_ranges_in_preference_order() -> None:
    ranges = parse_accept_language("en;q=0.8, fr;q=0.9")

    assert match_language_ranges(ranges, ["en", "fr"]) == ("fr", "en")


def test_match_language_ranges_returns_each_tag_once() -> None:
    ranges = parse_accept_language("fr, fr-be, *")

    assert match_language_ranges(ranges, ["fr-be"]) == ("fr-be",)


# --- §5.2 match depth and ranking -------------------------------------------------------


@pytest.mark.parametrize(
    ("range_tag", "tag", "expected"),
    [
        ("fr", "fr", 1),  # exact
        ("fr", "fr-be", 1),  # range widens onto a more specific tag (RFC 4647 Filtering)
        ("fr-be", "fr", 1),  # range narrows onto a less specific tag (Lookup truncation)
        ("fr-be", "fr-be", 2),  # exact, and more specific than either of the above
        ("fr-latn-fr", "fr", 1),  # three subtags deep against one
        ("fr", "fr-latn-fr", 1),  # and the same the other way round
        ("zh-hant", "zh-hant-tw", 2),
        ("zh-hant-tw", "zh-hans-cn", None),  # same language, different script
        ("fr", "frr", None),  # shares no whole subtag. Northern Frisian is not French
        ("frr", "fr", None),  # and it is not symmetric by accident
        ("fr-be", "fr-ca", None),  # shares `fr`, but neither is a prefix of the other
        ("fr", "de", None),
        ("fr", "", None),
        ("", "fr", None),
    ],
)
def test_match_depth(range_tag: str, tag: str, expected: int | None) -> None:
    assert match_depth(range_tag, tag) is expected


def test_rank_prefers_the_higher_quality_range() -> None:
    """A catalog offering both languages is ranked on the one the client wants more."""
    ranges = parse_accept_language("en;q=0.8, fr;q=0.9")

    assert rank_language_match(ranges, ["en", "fr"]) == LanguageMatch(rank=0, depth=1)


def test_rank_respects_header_order_when_every_q_is_equal() -> None:
    """`fr, en` states a preference through order alone, both ranges are q=1.0.

    Ranking on `q` would tie these two, and the feed would fall back to title order.
    """
    ranges = parse_accept_language("fr, en")

    assert rank_language_match(ranges, ["fr"]) == LanguageMatch(rank=0, depth=1)
    assert rank_language_match(ranges, ["en"]) == LanguageMatch(rank=1, depth=1)


def test_a_regional_range_matches_a_plain_tag() -> None:
    """`fr-FR` asked for, `fr` held, the registry must still match."""
    assert rank_language_match(parse_accept_language("fr-FR"), ["fr"]) == LanguageMatch(
        rank=0, depth=1
    )


def test_rank_prefers_the_more_specific_tag_within_one_range() -> None:
    ranges = parse_accept_language("fr-be")

    assert rank_language_match(ranges, ["fr", "fr-be"]) == LanguageMatch(rank=0, depth=2)


def test_preference_outranks_specificity() -> None:
    """The client's stated preference beats a more precise answer it wanted less.

    `en-gb` matches at depth 2 and `fr` only at depth 1, but the client put French first.
    Depth must never promote a language across a preference boundary.
    """
    ranges = parse_accept_language("en-gb;q=0.8, fr;q=0.9")
    precise_english = rank_language_match(ranges, ["en-gb"])
    plain_french = rank_language_match(ranges, ["fr"])

    assert plain_french == LanguageMatch(rank=0, depth=1)
    assert precise_english == LanguageMatch(rank=1, depth=2)
    assert plain_french.sort_key < precise_english.sort_key  # French sorts first


def test_a_wildcard_matches_at_the_lowest_specificity() -> None:
    """`*` matches anything, so an explicit range must always outrank it."""
    explicit = rank_language_match(parse_accept_language("fr"), ["fr"])
    wildcard = rank_language_match(parse_accept_language("*"), ["fr"])

    assert wildcard == LanguageMatch(rank=0, depth=0)
    assert explicit.sort_key < wildcard.sort_key


def test_rank_returns_none_when_nothing_matches() -> None:
    assert rank_language_match(parse_accept_language("de"), ["fr", "en"]) is None


def test_rank_returns_none_for_a_catalog_with_no_tags() -> None:
    """The service treats "no tags" separately; the pure function reports no match."""
    assert rank_language_match(parse_accept_language("fr"), []) is None


def test_rank_normalises_case_on_both_sides() -> None:
    assert rank_language_match(parse_accept_language("FR-BE"), ["Fr-Be"]) == LanguageMatch(
        rank=0, depth=2
    )


# --- ranking across several ranges ------------------------------------------------------


def test_rank_uses_every_range_in_the_header() -> None:
    """All ranges are considered, not just the first, each catalog lands where it matched."""
    ranges = parse_accept_language("fr;q=0.9, de;q=0.8, en;q=0.7")

    assert rank_language_match(ranges, ["fr"]) == LanguageMatch(rank=0, depth=1)
    assert rank_language_match(ranges, ["de"]) == LanguageMatch(rank=1, depth=1)
    assert rank_language_match(ranges, ["en"]) == LanguageMatch(rank=2, depth=1)


def test_rank_takes_the_best_position_a_catalog_can_reach() -> None:
    """A catalog holding several languages is ranked on whichever scores highest."""
    ranges = parse_accept_language("fr, de, en")

    assert rank_language_match(ranges, ["en", "de"]) == LanguageMatch(rank=1, depth=1)
    assert rank_language_match(ranges, ["en", "fr", "de"]) == LanguageMatch(rank=0, depth=1)


def test_a_wildcard_keeps_its_own_position_in_the_order() -> None:
    """`fr, *` ranks French at 0 and everything else at 1, the wildcard does not promote."""
    ranges = parse_accept_language("fr, *")

    assert rank_language_match(ranges, ["fr"]) == LanguageMatch(rank=0, depth=1)
    assert rank_language_match(ranges, ["de"]) == LanguageMatch(rank=1, depth=0)


def test_a_leading_wildcard_still_loses_to_a_specific_match_on_the_same_language() -> None:
    ranges = parse_accept_language("*, fr")

    assert rank_language_match(ranges, ["de"]) == LanguageMatch(rank=0, depth=0)
    # `fr` matches the wildcard at rank 0 depth 0 and the explicit range at rank 1 depth 1;
    # rank is compared first, so the wildcard match wins. The wildcard was asked for first.
    assert rank_language_match(ranges, ["fr"]) == LanguageMatch(rank=0, depth=0)


def test_a_zero_quality_range_cannot_rank_anything() -> None:
    """`q=0` means not acceptable (RFC 9110 §12.4.2), so it never ranks a catalog."""
    ranges = parse_accept_language("fr;q=0, en")

    assert [range_.tag for range_ in ranges] == ["en", "fr"]
    assert rank_language_match(ranges, ["fr"]) is None
    assert rank_language_match(ranges, ["en"]) == LanguageMatch(rank=0, depth=1)


def test_rejecting_every_language_is_not_the_same_as_stating_nothing() -> None:
    """`*;q=0` says "none of these", which must still filter. `;;;,,,` says nothing."""
    assert parse_accept_language("*;q=0") != ()
    assert rank_language_match(parse_accept_language("*;q=0"), ["fr"]) is None
    assert parse_accept_language(";;;,,,") == ()


def test_no_ranges_matches_nothing() -> None:
    """An empty range list is "no preference stated", the service skips ranking entirely."""
    assert rank_language_match((), ["fr"]) is None


def test_rank_is_stable_across_repeated_calls() -> None:
    """Determinism, which is what makes byte-identical responses possible."""
    ranges = parse_accept_language("fr;q=0.9, en;q=0.8")
    once = rank_language_match(ranges, ["en", "fr"])

    assert once == rank_language_match(ranges, ["en", "fr"])
    assert once == rank_language_match(ranges, ["fr", "en"])  # tag order is not preference


def test_the_range_cap_bounds_the_work_a_client_can_ask_for() -> None:
    """MAX_LANGUAGE_RANGES is what stops header length being a lever on server CPU."""
    header = ", ".join(f"x{index}" for index in range(MAX_LANGUAGE_RANGES + 25))

    assert len(parse_accept_language(header)) == MAX_LANGUAGE_RANGES
