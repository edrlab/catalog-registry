"""Who may do what in the admin API.

Mirrors the shared permissions matrix. The role table is the only authority: a permission is
necessary, never sufficient, because the catalog lifecycle must also allow the action
(`domain/lifecycle.py`).
"""

from collections.abc import Mapping
from typing import Final

from registry.domain.enums import ActorRole

VIEWER_PERMISSIONS: Final = frozenset({"backoffice.view", "catalog.view", "country.view"})

EDITOR_PERMISSIONS: Final = VIEWER_PERMISSIONS | {
    "catalog.create",
    "catalog.edit",
    "catalog.publish",
    "catalog.unpublish",
    "catalog.archive",
    "catalog.unarchive",
    "catalog.delete",
    "catalog.recommend",
}

ADMIN_PERMISSIONS: Final = EDITOR_PERMISSIONS | {
    "country.manage_subdivisions",
    "user.view",
    "user.change_role",
    "user.deactivate",
}

ROLE_PERMISSIONS: Final[Mapping[ActorRole, frozenset[str]]] = {
    ActorRole.VIEWER: VIEWER_PERMISSIONS,
    ActorRole.EDITOR: EDITOR_PERMISSIONS,
    ActorRole.ADMIN: ADMIN_PERMISSIONS,
}


def build_permissions(role: ActorRole) -> frozenset[str]:
    return ROLE_PERMISSIONS[role]
