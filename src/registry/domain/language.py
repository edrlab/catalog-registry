"""Language range parsing and matching. Pure. Stdlib only.

Two specifications, and the difference between them is where the bugs live:

* **RFC 9110 §12.5.4**. How an ``Accept-Language`` header is written and weighted.
* **RFC 4647 §3.3.1 (Filtering) and §3.4 (Lookup)**. How a range is compared to a tag.
  Matching is on *subtag* boundaries, not characters: ``fr`` matches ``fr-be`` and does
  **not** match ``frr`` (Northern Frisian). A naive ``startswith`` gets that wrong.

  Neither RFC procedure alone is right here, and taking one off the shelf is the trap.
  Filtering only widens (range ``fr`` matches tag ``fr-be``); Lookup only narrows (range
  ``fr-be`` matches tag ``fr``, by truncating the range). A registry needs both: a reader
  asking for ``fr`` should be offered a ``fr-be`` catalog, and a reader asking for ``fr-be``
  should be offered a ``fr`` one. So the rule is **whole-subtag prefix in either
  direction**, and ``fr-be`` still does not match ``fr-ca``. They share a prefix, but
  neither *is* a prefix of the other.

Ranges are compared as strings in practice even though BCP-47 declares tags
case-insensitive, so everything is lowercased on the way in.
"""

from collections.abc import Iterable, Sequence

from registry.domain.models import LanguageMatch, LanguageRange

__all__ = [
    "MAX_LANGUAGE_RANGES",
    "LanguageMatch",
    "LanguageRange",
    "is_wildcard_range",
    "match_language_ranges",
    "normalise_language_tag",
    "parse_accept_language",
    "rank_language_match",
]

WILDCARD = "*"

#: RFC 9110 §12.5.4 gives quality values at most three decimal places.
QUALITY_DECIMAL_PLACES = 3

#: An upper bound on how much of a header is parsed. A client can send an arbitrarily long
#: Accept-Language; parsing it unboundedly is free work an unauthenticated caller controls.
MAX_LANGUAGE_RANGES = 50


def normalise_language_tag(tag: str) -> str:
    """Canonical comparison form: lowercase, hyphen-separated, trimmed. Idempotent."""
    return tag.strip().lower().replace("_", "-")


def is_wildcard_range(range_: LanguageRange) -> bool:
    return range_.tag == WILDCARD


def _parse_quality(parameters: Sequence[str]) -> float:
    """Read the ``q=`` parameter.

    A malformed value is treated as ``q=1.0`` rather than raising: RFC 9110 §12.4.2 gives no
    licence to reject the whole header, and a 500 on a bad header would be worse than
    serving an unfiltered feed. Out-of-range values are clamped into [0, 1].
    """
    for parameter in parameters:
        name, _, value = parameter.partition("=")
        if name.strip().lower() != "q":
            continue
        try:
            quality = float(value.strip())
        except ValueError:
            return 1.0
        quality = min(max(quality, 0.0), 1.0)
        return round(quality, QUALITY_DECIMAL_PLACES)
    return 1.0


def parse_accept_language(header: str | None) -> tuple[LanguageRange, ...]:
    """Parse an ``Accept-Language`` header into ranges, most preferred first.

    RFC 9110 §12.5.4. Returns ``()`` for a missing, empty, or wholly unparseable header,
    which callers read as "no preference stated", not as "nothing is acceptable".

    ``q=0`` means *not acceptable* (§12.4.2). Such ranges are dropped here, so a tag that
    only they would have matched is simply never matched.
    """
    if not header:
        return ()

    ranges: list[LanguageRange] = []
    for element in header.split(",")[:MAX_LANGUAGE_RANGES]:
        tag, *parameters = element.split(";")
        normalised = normalise_language_tag(tag)
        if not normalised:
            continue
        quality = _parse_quality(parameters)
        if quality == 0.0:
            continue
        ranges.append(LanguageRange(tag=normalised, quality=quality))

    # Stable sort: equal quality preserves the order the client wrote.
    return tuple(sorted(ranges, key=lambda range_: -range_.quality))


def _strip_singleton_extensions(tag: str) -> str:
    """Drop a singleton subtag and everything after it (RFC 4647 §3.4).

    ``de-a-value`` carries an extension, not a more specific language, so it must still
    match the tag ``de``. Position 0 is never a singleton in a well-formed tag.
    """
    subtags = tag.split("-")
    for index, subtag in enumerate(subtags[1:], start=1):
        if len(subtag) == 1:
            return "-".join(subtags[:index])
    return tag


def match_depth(range_tag: str, available_tag: str) -> int | None:
    """Subtags shared by *range_tag* and *available_tag*, or ``None`` when they do not match.

    A match requires one to be a whole-subtag prefix of the other, the union of RFC 4647
    Filtering and Lookup, for the reason in the module docstring. The returned depth is how
    many subtags the two share, which is what makes one match rankable against another:
    ``fr-be`` against ``fr-be`` (2) is a better answer than ``fr`` against ``fr-be`` (1).

    ``fr`` and ``frr`` share no subtag and do not match. ``fr-be`` and ``fr-ca`` share one,
    but neither is a prefix of the other, so they do not match either.
    """
    left, right = range_tag.split("-"), available_tag.split("-")
    depth = 0
    for one, other in zip(left, right, strict=False):
        if one != other:
            break
        depth += 1
    if depth and depth in (len(left), len(right)):
        return depth
    return None


def match_language_ranges(
    ranges: Sequence[LanguageRange], available: Iterable[str]
) -> tuple[str, ...]:
    """Return the available tags acceptable to *ranges*, in order of preference.

    Preference order is range order first, then the order the tags were supplied. Each tag
    appears once. An empty result means the caller stated a preference and none of it is
    available, which is different from stating no preference at all.
    """
    tags = [normalise_language_tag(tag) for tag in available]
    matched: dict[str, None] = {}

    for range_ in ranges:
        if is_wildcard_range(range_):
            matched.update(dict.fromkeys(tags))
            continue
        range_tag = _strip_singleton_extensions(range_.tag)
        matched.update(
            dict.fromkeys(tag for tag in tags if match_depth(range_tag, tag) is not None)
        )

    return tuple(matched)


def rank_language_match(
    ranges: Sequence[LanguageRange], available: Iterable[str]
) -> LanguageMatch | None:
    """How well *available* answers *ranges*, or ``None`` when nothing matches.

    The best match wins, not the first, and it is ranked on the range's *position* rather
    than its ``q``, because ``Accept-Language: fr, en`` states a preference with order alone.

    ponytail: linear scan, not an index. A prefix map of the ranges built once per request
    would make this O(t · s) per catalog, worth doing when `n` reaches the thousands, the
    search path (v0.2), not this one.
    """
    tags = [normalise_language_tag(tag) for tag in available]
    best: LanguageMatch | None = None

    for position, range_ in enumerate(ranges):
        range_tag = _strip_singleton_extensions(range_.tag)
        for tag in tags:
            depth = 0 if is_wildcard_range(range_) else match_depth(range_tag, tag)
            if depth is None:
                continue
            candidate = LanguageMatch(rank=position, depth=depth)
            if best is None or candidate.sort_key < best.sort_key:
                best = candidate

    return best
