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


#: What an admin may do to another person (`allowed_actions`).
USER_ACTIONS: Final = ("change_role", "deactivate", "reactivate")


def build_user_actions(
    caller_id: str,
    caller_permissions: frozenset[str],
    *,
    user_id: str,
    user_role: ActorRole,
    user_is_active: bool,
    active_admins: int,
) -> list[str]:
    """What the caller may do to this user now. Never to yourself; never demote or deactivate the
    last active admin; deactivate only someone active, reactivate only someone not. The endpoints
    enforce the same rules; this is what the UI builds its menu from."""
    if user_id == caller_id:
        return []
    last_admin = user_role is ActorRole.ADMIN and user_is_active and active_admins <= 1
    actions = []
    if "user.change_role" in caller_permissions and not last_admin:
        actions.append("change_role")
    if "user.deactivate" in caller_permissions:
        if user_is_active and not last_admin:
            actions.append("deactivate")
        if not user_is_active:
            actions.append("reactivate")
    return actions
