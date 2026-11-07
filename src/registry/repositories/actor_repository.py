"""The admin API's people. Writes: a login records itself, and a first login creates a viewer."""

from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.domain.admin_models import AdminActor, AdminUser
from registry.domain.enums import ActorRole

#: One statement for "find or create, and note the login". `role` is never in the update list, so a
#: login can never change a role, and an existing `name` is kept over the token's. A write per
#: request is fine at this volume; if it ever is not, update `last_seen_at` only when it is stale.
_PERSIST_LOGIN: Final = text(
    """
WITH previous AS (SELECT last_seen_at FROM actors WHERE email = :email)
INSERT INTO actors (email, name, identity_issuer, identity_subject, last_seen_at)
VALUES (:email, :name, :issuer, :subject, now())
ON CONFLICT (email) DO UPDATE
   SET last_seen_at = now(),
       identity_issuer = EXCLUDED.identity_issuer,
       identity_subject = EXCLUDED.identity_subject,
       name = coalesce(actors.name, EXCLUDED.name)
RETURNING id::text AS id, email, name, role::text AS role, is_active, created_at,
          identity_issuer, identity_subject, (SELECT last_seen_at FROM previous) AS previous_seen_at
"""
)


_USER_COLUMNS: Final = (
    "id::text AS id, email, name, role::text AS role, is_active, last_seen_at, created_at"
)
_USERS: Final = text(f"SELECT {_USER_COLUMNS} FROM actors ORDER BY is_active DESC, email")
#: Every change locks the active admins first, always in id order, then counts them in the same
#: transaction: two admins demoting each other cannot both succeed.
_LOCK_ACTIVE_ADMINS: Final = text(
    "SELECT id::text FROM actors WHERE role = 'admin' AND is_active ORDER BY id FOR UPDATE"
)
_LOCK_USER: Final = text(
    f"SELECT {_USER_COLUMNS} FROM actors WHERE id = CAST(:id AS uuid) FOR UPDATE"
)
_SET_ROLE: Final = text(
    "UPDATE actors SET role = CAST(:role AS actor_role) WHERE id = CAST(:id AS uuid)"
)
_SET_NAME: Final = text("UPDATE actors SET name = :name WHERE id = CAST(:id AS uuid)")
_SET_ACTIVE: Final = text("UPDATE actors SET is_active = :active WHERE id = CAST(:id AS uuid)")


def _build_user(row: Any) -> AdminUser:
    return AdminUser(
        id=row.id,
        email=row.email,
        name=row.name,
        role=ActorRole(row.role),
        is_active=row.is_active,
        last_seen_at=row.last_seen_at,
        created_at=row.created_at,
    )


class ActorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist_actor_login(
        self, *, email: str, name: str | None, issuer: str, subject: str | None
    ) -> AdminActor:
        row = (
            (
                await self._session.execute(
                    _PERSIST_LOGIN,
                    {"email": email, "name": name, "issuer": issuer, "subject": subject},
                )
            )
            .mappings()
            .one()
        )
        await self._session.commit()
        return AdminActor(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            role=ActorRole(row["role"]),
            is_active=row["is_active"],
            created_at=row["created_at"],
            previous_seen_at=row["previous_seen_at"],
            identity_issuer=row["identity_issuer"],
            identity_subject=row["identity_subject"],
        )

    async def persist_actor_name(self, actor_id: str, name: str | None) -> None:
        await self._session.execute(_SET_NAME, {"id": actor_id, "name": name})
        await self._session.commit()

    async def fetch_admin_users(self) -> list[AdminUser]:
        return [_build_user(row) for row in await self._session.execute(_USERS)]

    async def lock_active_admins(self) -> list[str]:
        return list((await self._session.execute(_LOCK_ACTIVE_ADMINS)).scalars())

    async def lock_admin_user(self, user_id: str) -> AdminUser | None:
        row = (await self._session.execute(_LOCK_USER, {"id": user_id})).first()
        return _build_user(row) if row else None

    async def persist_user_role(self, user_id: str, role: ActorRole) -> None:
        await self._session.execute(_SET_ROLE, {"id": user_id, "role": role.value})

    async def persist_user_active(self, user_id: str, active: bool) -> None:
        await self._session.execute(_SET_ACTIVE, {"id": user_id, "active": active})

    async def commit_user_change(self) -> None:
        await self._session.commit()
