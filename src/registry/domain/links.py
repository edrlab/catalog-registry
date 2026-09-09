"""Link ordering. Pure — stdlib only.

Ordering is computed from `rel` rather than stored, so it cannot drift from the spec the
moment the spec changes. The renderer dispatches through this map rather than an if/elif
chain: adding a rel means adding a dict entry, not editing a function.
"""

from collections.abc import Callable, Sequence
from types import MappingProxyType

from registry.domain.enums import LinkRel

#: Most useful to a client first: where to browse, then where their own shelf is, then how
#: to search and sign in, then the decorative and alternate representations.
LINK_REL_PRIORITY = MappingProxyType(
    {
        LinkRel.SELF: 0,
        LinkRel.CATALOG: 1,
        LinkRel.SHELF: 2,
        LinkRel.SEARCH: 3,
        LinkRel.AUTHENTICATE: 4,
        LinkRel.PROFILE: 5,
        LinkRel.ALTERNATE: 6,
        LinkRel.ICON: 7,
    }
)


def order_links[T](links: Sequence[T], *, rel_of: Callable[[T], LinkRel]) -> tuple[T, ...]:
    """Stable sort on `LINK_REL_PRIORITY`. Two links sharing a rel keep their input order."""
    return tuple(sorted(links, key=lambda link: LINK_REL_PRIORITY[rel_of(link)]))


def find_self_link[T](links: Sequence[T], *, rel_of: Callable[[T], LinkRel]) -> T | None:
    return next((link for link in links if rel_of(link) is LinkRel.SELF), None)


def has_browsable_rel(rels: Sequence[LinkRel]) -> bool:
    """A catalog needs somewhere to actually browse or borrow.

    `self` is deliberately not checked: it points at this registry, so it is synthesised at
    render time rather than supplied by whoever authored the catalog (ADR-030).
    """
    return bool(set(rels) & {LinkRel.CATALOG, LinkRel.SHELF})
