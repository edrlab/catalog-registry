"""The catalog status state machine, in one place.

Clients never re-implement it: they render the `allowed_actions` computed here. Transitions
and visibility are the shared catalog lifecycle contract.

    draft ──publish──► published ◄──publish── unpublished
                           │  ──unpublish──►      │
                         archive                archive
                           ▼                      ▼
                        archived ──unarchive──► unpublished

A draft can be deleted; nothing else can. Unarchiving never lands on published.
"""

from collections.abc import Mapping
from typing import Final

from registry.domain.enums import CatalogStatus

#: Catalog actions in the order the API lists them.
ACTIONS: Final = (
    "edit",
    "publish",
    "unpublish",
    "archive",
    "unarchive",
    "delete",
    "recommend",
)

#: What the lifecycle allows from each status, before the caller's role is considered. An archived
#: catalog is read-only, so it offers no `edit`; `recommend` needs a published catalog.
STATUS_ACTIONS: Final[Mapping[CatalogStatus, frozenset[str]]] = {
    CatalogStatus.DRAFT: frozenset({"edit", "publish", "delete"}),
    CatalogStatus.PUBLISHED: frozenset({"edit", "unpublish", "archive", "recommend"}),
    CatalogStatus.UNPUBLISHED: frozenset({"edit", "publish", "archive"}),
    CatalogStatus.ARCHIVED: frozenset({"unarchive"}),
}


def build_allowed_actions(status: CatalogStatus, permissions: frozenset[str]) -> list[str]:
    """Actions this caller may take on a catalog in *status* now: lifecycle ∩ role."""
    return [
        action
        for action in ACTIONS
        if action in STATUS_ACTIONS[status] and f"catalog.{action}" in permissions
    ]
