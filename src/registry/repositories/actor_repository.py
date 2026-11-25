"""The admin API's people. Writes: a login records itself, and a first login creates a viewer."""

from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.domain.admin_models import AdminActor, AdminUser
from registry.domain.enums import ActorRole

#: One statement for "find or create, and note the login". `role` is never in the update list, so a
#: login can never change a role, and an existing `name` is kept over the token's. The first
#: identity is kept too, as a pair: the first login that brings a subject records its issuer with
#: it (a dev login before it has none). The service refuses a later login that brings another
#: identity, so a reissued mailbox does not inherit the account, and such a login writes nothing.
#:
#: `last_seen_at` is written at most once a minute: every admin request comes through here, and most
#: are page views that need no write. Within the minute the row is read back unchanged (the second
#: half of the UNION), so "previous visit" means the last one more than a minute ago.
_PERSIST_LOGIN: Final = text(
    """
WITH previous AS (SELECT last_seen_at FROM actors WHERE email = :email),
noted AS (
  INSERT INTO actors (email, name, identity_issuer, identity_subject, last_seen_at)
  VALUES (:email, :name, :issuer, :subject, now())
  ON CONFLICT (email) DO UPDATE
     SET last_seen_at = now(),
         identity_issuer = CASE WHEN actors.identity_subject IS NULL
                                 AND EXCLUDED.identity_subject IS NOT NULL
                                THEN EXCLUDED.identity_issuer
                                ELSE coalesce(actors.identity_issuer, EXCLUDED.identity_issuer)
                           END,
         identity_subject = coalesce(actors.identity_subject, EXCLUDED.identity_subject),
         name = coalesce(actors.name, EXCLUDED.name)
   WHERE (actors.last_seen_at IS NULL
          OR actors.last_seen_at < now() - interval '1 minute'
          OR (actors.identity_subject IS NULL AND EXCLUDED.identity_subject IS NOT NULL)
          OR (actors.name IS NULL AND EXCLUDED.name IS NOT NULL))
     AND (actors.identity_subject IS NULL OR EXCLUDED.identity_subject IS NULL
          OR (actors.identity_issuer, actors.identity_subject)
             = (EXCLUDED.identity_issuer, EXCLUDED.identity_subject))
  RETURNING id::text AS id, email, name, role::text AS role, is_active, created_at,
            identity_issuer, identity_subject,
            (SELECT last_seen_at FROM previous) AS previous_seen_at
)
SELECT * FROM noted
UNION ALL
SELECT id::text, email, name, role::text, is_active, created_at,
       identity_issuer, identity_subject, last_seen_at
  FROM actors
 WHERE email = :email AND NOT EXISTS (SELECT 1 FROM noted)
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
        params = {"email": email, "name": name, "issuer": issuer, "subject": subject}
        row = (await self._session.execute(_PERSIST_LOGIN, params)).mappings().first()
        if row is None:
            # A first login for this email committed while this one waited on it: the update was
            # skipped and the read-back's snapshot predates the row. A new statement sees it.
            row = (await self._session.execute(_PERSIST_LOGIN, params)).mappings().one()
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
