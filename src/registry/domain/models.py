"""The domain's own types. Pure, frozen, stdlib only.

Frozen because they cross layer boundaries, and a mutable object that crosses a boundary
eventually gets mutated on the far side.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class LanguageRange:
    """One range from an ``Accept-Language`` header (RFC 9110 §12.5.4)."""

    tag: str
    quality: float = 1.0


@dataclass(frozen=True, slots=True)
class LanguageMatch:
    """How well one catalog answers an ``Accept-Language`` header.

    ``rank`` is the winning range's position in the client's preference order, not its ``q``;
    ``depth`` is how many subtags the match shares, breaking ties within a single range.
    """

    rank: int
    depth: int

    @property
    def sort_key(self) -> tuple[int, int]:
        """Ascending-sort key: best match first."""
        return (self.rank, -self.depth)


@dataclass(frozen=True, slots=True)
class DomainCatalog:
    """One catalog, as the feed needs it.

    ``document`` is the catalog's OPDS representation, already conforming to
    ``schema/catalog.schema.json``. It is carried rather than rebuilt because in this
    version the source of truth *is* that document.

    ponytail: no whitelist projection yet, there is no internal field to leak while the
    source is a file that is already the wire format. Whitelist projection lands with the
    database, in the same change that introduces columns the wire contract does not have.
    """

    languages: tuple[str, ...] = ()
    document: dict[str, Any] = field(default_factory=dict)
