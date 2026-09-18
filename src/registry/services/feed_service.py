"""The top-level feed: fetch, filter by language, render.

Depends on the `CatalogReader` protocol, so its tests use an in-memory fake and touch no
database.
"""

from collections.abc import Sequence
from typing import Any

from registry.db.models.catalog import Catalog
from registry.domain.language import LanguageRange, parse_accept_language, rank_language_match
from registry.rendering.feed_renderer import render_feed
from registry.repositories.protocols import CatalogReader

#: A catalog matching the client's language leads; one scoped to no language trails every
#: match. Buckets rather than a sentinel score, so position never depends on the `q` values
#: in the header. `created_at` (newest first) breaks ties within a bucket.
_MATCHED, _UNSCOPED = 0, 1


def _rank(catalog: Catalog, ranges: Sequence[LanguageRange]) -> tuple[int, int, int, float] | None:
    """Sort key for *catalog*, or ``None`` when the request excludes it.

    A catalog declaring no languages is never excluded, since it has made no claim to
    contradict, but it sorts below every catalog scoped to a specific language that matched.
    The final element is `-created_at.timestamp()`, so within a bucket (and within a single
    range's rank/depth) the newest catalog sorts first.
    """
    if not catalog.languages:
        return (_UNSCOPED, 0, 0, -catalog.created_at.timestamp())
    match = rank_language_match(ranges, [row.language_tag for row in catalog.languages])
    if match is None:
        return None
    return (_MATCHED, *match.sort_key, -catalog.created_at.timestamp())


async def resolve_top_level_feed(
    reader: CatalogReader, *, accept_language: str | None, base_url: str
) -> dict[str, Any]:
    """Return the recommended catalogs acceptable to *accept_language*, best match first.

    No ranges means no filtering and no reordering, the client stated no preference — the
    feed keeps the repository's `created_at DESC, title` order as-is.

    The sort is *stable*, so catalogs ranking exactly equally (same bucket, rank, depth, and
    `created_at`) keep the order the repository established, and two identical requests
    produce byte-identical bodies without this function knowing anything about titles.
    """
    catalogs = await reader.fetch_recommended_catalogs()
    ranges = parse_accept_language(accept_language)
    if not ranges:
        return render_feed(catalogs, base_url=base_url)

    ranked = [(rank, catalog) for catalog in catalogs if (rank := _rank(catalog, ranges))]
    ranked.sort(key=lambda pair: pair[0])
    return render_feed([catalog for _, catalog in ranked], base_url=base_url)
