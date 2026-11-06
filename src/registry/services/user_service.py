"""People, for admins (contract 0.9): the list, a role change, deactivation and reactivation.

A change runs in one transaction: lock the active admins (in id order), check the caller is still
one of them, lock the person, apply the rules, write, commit. Two admins demoting each other at once
are applied one after the other, and the second meets `last-admin`.
"""

import dataclasses
import uuid

from registry.core.errors import (
    LastAdminError,
    PermissionDeniedError,
    SelfChangeError,
    UserNotFoundError,
)
from registry.domain.admin_models import AdminActor, AdminUser
from registry.domain.enums import ActorRole
from registry.domain.permissions import build_permissions, build_user_actions
from registry.repositories.protocols import UserStore
from registry.schemas.admin import UserItem, UserListResponse


def _build_user_item(actor: AdminActor, user: AdminUser, active_admins: int) -> UserItem:
    return UserItem(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role.value,
        is_active=user.is_active,
        last_seen_at=user.last_seen_at,
        created_at=user.created_at,
        allowed_actions=build_user_actions(
            actor.id,
            build_permissions(actor.role),
            user_id=user.id,
            user_role=user.role,
            user_is_active=user.is_active,
            active_admins=active_admins,
        ),
    )


async def resolve_admin_users(store: UserStore, actor: AdminActor) -> UserListResponse:
    users = await store.fetch_admin_users()
    admins = sum(1 for user in users if user.role is ActorRole.ADMIN and user.is_active)
    return UserListResponse(items=[_build_user_item(actor, user, admins) for user in users])


async def _lock_target(store: UserStore, actor: AdminActor, user_id: str) -> tuple[AdminUser, int]:
    """The person, locked, and how many active admins there are, also locked. Raises 404, 409
    `self-change`, or 403 when the caller stopped being an active admin in the meantime."""
    try:
        normalised = str(uuid.UUID(user_id))
    except ValueError:
        raise UserNotFoundError(f"No user with id {user_id}.") from None
    if normalised == actor.id:
        raise SelfChangeError("You cannot change your own role or access.")
    admins = await store.lock_active_admins()
    if actor.id not in admins:
        raise PermissionDeniedError("Your access changed; reload the page.")
    user = await store.lock_admin_user(normalised)
    if user is None:
        raise UserNotFoundError(f"No user with id {normalised}.")
    return user, len(admins)


def _is_last_admin(user: AdminUser, active_admins: int) -> bool:
    return user.role is ActorRole.ADMIN and user.is_active and active_admins <= 1


async def apply_user_role_change(
    store: UserStore, actor: AdminActor, user_id: str, role: ActorRole
) -> UserItem:
    user, admins = await _lock_target(store, actor, user_id)
    if user.role is not role:
        if role is not ActorRole.ADMIN and _is_last_admin(user, admins):
            raise LastAdminError("This is the last active admin; make someone else admin first.")
        await store.persist_user_role(user.id, role)
        await store.commit_user_change()
        changed = dataclasses.replace(user, role=role)
        admins, user = _count_admins_after(admins, user, changed), changed
    return _build_user_item(actor, user, admins)


async def apply_user_activation(
    store: UserStore, actor: AdminActor, user_id: str, *, active: bool
) -> UserItem:
    user, admins = await _lock_target(store, actor, user_id)
    if user.is_active is not active:
        if not active and _is_last_admin(user, admins):
            raise LastAdminError("This is the last active admin; it cannot be deactivated.")
        await store.persist_user_active(user.id, active)
        await store.commit_user_change()
        changed = dataclasses.replace(user, is_active=active)
        admins, user = _count_admins_after(admins, user, changed), changed
    return _build_user_item(actor, user, admins)


def _count_admins_after(admins: int, before: AdminUser, after: AdminUser) -> int:
    def counts(user: AdminUser) -> int:
        return int(user.role is ActorRole.ADMIN and user.is_active)

    return admins - counts(before) + counts(after)
