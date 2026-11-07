"""Role changes and deactivation, against a fake `UserStore`.

Through the API the caller is always an active admin other than the person changed, so the store
always counts at least two admins. These cases give the service a store that says otherwise, to pin
the rules it enforces whatever the store reports.
"""

import datetime
import uuid

import pytest

from registry.core.errors import LastAdminError
from registry.domain.admin_models import AdminActor, AdminUser
from registry.domain.enums import ActorRole
from registry.services.user_service import apply_user_activation, apply_user_role_change

CALLER = AdminActor(
    id=str(uuid.uuid4()), email="a@edrlab.org", name=None, role=ActorRole.ADMIN, is_active=True
)
TARGET_ID = str(uuid.uuid4())


def person(role: ActorRole, *, active: bool = True) -> AdminUser:
    return AdminUser(
        id=TARGET_ID,
        email="p@edrlab.org",
        name=None,
        role=role,
        is_active=active,
        last_seen_at=None,
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )


class FakeUserStore:
    """Locks report *admins* and *user*; every write is recorded."""

    def __init__(self, user: AdminUser, admins: list[str]) -> None:
        self.user = user
        self.admins = admins
        self.writes: list[tuple[str, object]] = []

    async def fetch_admin_users(self) -> list[AdminUser]:
        return [self.user]

    async def lock_active_admins(self) -> list[str]:
        return self.admins

    async def lock_admin_user(self, user_id: str) -> AdminUser | None:
        return self.user if user_id == self.user.id else None

    async def persist_user_role(self, user_id: str, role: ActorRole) -> None:
        self.writes.append(("role", role))

    async def persist_user_active(self, user_id: str, active: bool) -> None:
        self.writes.append(("active", active))

    async def commit_user_change(self) -> None:
        self.writes.append(("commit", None))


async def test_the_last_active_admin_cannot_be_demoted() -> None:
    store = FakeUserStore(person(ActorRole.ADMIN), admins=[CALLER.id])

    with pytest.raises(LastAdminError):
        await apply_user_role_change(store, CALLER, TARGET_ID, ActorRole.EDITOR)

    assert store.writes == []


async def test_the_last_active_admin_cannot_be_deactivated() -> None:
    store = FakeUserStore(person(ActorRole.ADMIN), admins=[CALLER.id])

    with pytest.raises(LastAdminError):
        await apply_user_activation(store, CALLER, TARGET_ID, active=False)

    assert store.writes == []


async def test_reactivating_someone_already_active_writes_nothing() -> None:
    store = FakeUserStore(person(ActorRole.EDITOR), admins=[CALLER.id])

    item = await apply_user_activation(store, CALLER, TARGET_ID, active=True)

    assert (item.is_active, store.writes) == (True, [])
