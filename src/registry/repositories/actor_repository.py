"""The admin API's people. Writes: a login records itself, and a first login creates a viewer."""

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.domain.admin_models import AdminActor
from registry.domain.enums import ActorRole

#: One statement for "find or create, and note the login". `role` is never in the update list, so a
#: login can never change a role, and an existing `name` is kept over the token's. A write per
#: request is fine at this volume; if it ever is not, update `last_seen_at` only when it is stale.
_PERSIST_LOGIN: Final = text(
    """
INSERT INTO actors (email, name, identity_issuer, identity_subject, last_seen_at)
VALUES (:email, :name, :issuer, :subject, now())
ON CONFLICT (email) DO UPDATE
   SET last_seen_at = now(),
       identity_issuer = EXCLUDED.identity_issuer,
       identity_subject = EXCLUDED.identity_subject,
       name = coalesce(actors.name, EXCLUDED.name)
RETURNING id::text AS id, email, name, role::text AS role, is_active
"""
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
        )
