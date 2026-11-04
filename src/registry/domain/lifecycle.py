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
from typing import Final, Literal

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


#: The actions `POST /admin/catalogs/{id}/transitions` takes; the others have their own routes.
TRANSITION_ACTIONS: Final = ("publish", "unpublish", "archive", "unarchive")

#: `(from, action) -> to`. Anything absent is refused as `invalid-transition`.
TRANSITIONS: Final[Mapping[tuple[CatalogStatus, str], CatalogStatus]] = {
    (CatalogStatus.DRAFT, "publish"): CatalogStatus.PUBLISHED,
    (CatalogStatus.PUBLISHED, "unpublish"): CatalogStatus.UNPUBLISHED,
    (CatalogStatus.PUBLISHED, "archive"): CatalogStatus.ARCHIVED,
    (CatalogStatus.UNPUBLISHED, "publish"): CatalogStatus.PUBLISHED,
    (CatalogStatus.UNPUBLISHED, "archive"): CatalogStatus.ARCHIVED,
    (CatalogStatus.ARCHIVED, "unarchive"): CatalogStatus.UNPUBLISHED,
}

#: The undo rule of the lifecycle contract, as its table lists it: an action can be undone only
#: when the inverse lands exactly on the previous status. Unarchive is left out on purpose: its
#: inverse, archive, is a deliberate step the back office asks to confirm.
UndoAction = Literal["publish", "unpublish", "unarchive"]

UNDO_ACTIONS: Final[Mapping[tuple[CatalogStatus, str], UndoAction]] = {
    (CatalogStatus.PUBLISHED, "unpublish"): "publish",
    (CatalogStatus.UNPUBLISHED, "publish"): "unpublish",
    (CatalogStatus.UNPUBLISHED, "archive"): "unarchive",
}


def build_allowed_actions(status: CatalogStatus, permissions: frozenset[str]) -> list[str]:
    """Actions this caller may take on a catalog in *status* now: lifecycle ∩ role."""
    return [
        action
        for action in ACTIONS
        if action in STATUS_ACTIONS[status] and f"catalog.{action}" in permissions
    ]
