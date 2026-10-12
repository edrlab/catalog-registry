"""The `restrict` query param: which catalogs an integrator's app or device can use. Pure.

`restrict=opds` keeps catalogs that have a `catalog` link of type `application/opds+json`
(OPDS 2.0 only). `restrict=no-open` drops any catalog with `open` among its kinds. Both may be
given, comma-separated. A restriction removes catalogs; it never changes their order.

The search SQL repeats these two rules as `WHERE` clauses (`search_repository.py`) because a
page and its total are counted in the database. The tests run both on the same data.
"""

from dataclasses import dataclass
from typing import Final

from registry.core.constants import OPDS_JSON_MEDIA_TYPE
from registry.domain.catalog_view import CatalogLike
from registry.domain.enums import CatalogKind, LinkRel

#: What the route accepts: empty, or a comma-separated list of the two values.
RESTRICT_PATTERN: Final = r"^((opds|no-open)(,(opds|no-open))*)?$"


@dataclass(frozen=True, slots=True)
class Restrictions:
    opds: bool = False
    no_open: bool = False

    @property
    def query_value(self) -> str | None:
        """The `restrict` value to put in a link, or ``None`` when nothing is restricted."""
        values = [name for name, on in (("opds", self.opds), ("no-open", self.no_open)) if on]
        return ",".join(values) or None


NO_RESTRICTIONS: Final = Restrictions()


def parse_restrictions(value: str | None) -> Restrictions:
    """Read a `restrict` value already checked against `RESTRICT_PATTERN`."""
    names = set((value or "").split(","))
    return Restrictions(opds="opds" in names, no_open="no-open" in names)


def is_opds2_media_type(media_type: str | None) -> bool:
    """RFC 9110 §8.3.1: the type is case-insensitive and parameters follow a semicolon."""
    return (media_type or "").split(";")[0].strip().lower() == OPDS_JSON_MEDIA_TYPE


def allows(catalog: CatalogLike, restrictions: Restrictions) -> bool:
    if restrictions.opds and not any(
        link.rel is LinkRel.CATALOG and is_opds2_media_type(link.media_type)
        for link in catalog.links
    ):
        return False
    return not (restrictions.no_open and any(row.kind is CatalogKind.OPEN for row in catalog.kinds))
