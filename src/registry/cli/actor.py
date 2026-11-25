"""Add someone to the admin API, or change their role or access.

    python -m registry.cli actor set someone@edrlab.org --role admin --name "Ada Lovelace"
    python -m registry.cli actor set former@edrlab.org --deactivate
    python -m registry.cli actor set former@edrlab.org --activate
    python -m registry.cli actor set someone@edrlab.org --forget-identity

The first administrator has to come from somewhere, and the CLI is trusted by holding the database
credential, as `seed` and `add` are. Everyone after that is a viewer on first login until an
administrator changes their role.
"""

import argparse
import asyncio
from collections.abc import Sequence

from sqlalchemy import text

from registry.core.config import Settings
from registry.core.errors import LastAdminError, ValidationError
from registry.db.session import build_session_factory, create_database_engine
from registry.domain.enums import ActorRole

#: `role`, `name` and `is_active` are applied only when given, so reactivating someone does not
#: reset their role, and changing a role does not reactivate someone. A new person is active.
_SET_ACTOR = text(
    """
INSERT INTO actors (email, name, role, is_active)
VALUES (:email, :name, CAST(coalesce(:role, 'viewer') AS actor_role),
        coalesce(CAST(:is_active AS boolean), true))
ON CONFLICT (email) DO UPDATE
   SET role = CAST(coalesce(:role, actors.role::text) AS actor_role),
       name = coalesce(:name, actors.name),
       is_active = coalesce(CAST(:is_active AS boolean), actors.is_active),
       identity_issuer = CASE WHEN CAST(:forget AS boolean) THEN NULL ELSE actors.identity_issuer END,
       identity_subject = CASE WHEN CAST(:forget AS boolean) THEN NULL ELSE actors.identity_subject END
RETURNING email, role::text AS role, is_active
"""
)

#: The active admins, locked in id order: the order the admin API locks them in, so a change here
#: and one there serialise instead of each removing "the other" last admin.
_LOCK_ACTIVE_ADMINS = text(
    "SELECT id FROM actors WHERE role = 'admin' AND is_active ORDER BY id FOR UPDATE"
)
_COUNT_ACTIVE_ADMINS = text("SELECT count(*) FROM actors WHERE role = 'admin' AND is_active")


def build_actor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m registry.cli actor")
    parser.add_argument("action", choices=["set"])
    parser.add_argument("email")
    parser.add_argument("--role", choices=[role.value for role in ActorRole])
    parser.add_argument("--name")
    access = parser.add_mutually_exclusive_group()
    access.add_argument(
        "--activate", dest="active", action="store_const", const=True, help="restore their access"
    )
    access.add_argument(
        "--deactivate", dest="active", action="store_const", const=False, help="remove their access"
    )
    parser.add_argument(
        "--forget-identity",
        dest="forget",
        action="store_true",
        help="let the next login record a new account for this email",
    )
    return parser


async def persist_actor(
    settings: Settings,
    *,
    email: str,
    role: str | None,
    name: str | None,
    active: bool | None,
    forget: bool = False,
) -> tuple[str, str, bool]:
    engine = create_database_engine(settings)
    try:
        async with build_session_factory(engine)() as session:
            admins_before = len((await session.execute(_LOCK_ACTIVE_ADMINS)).all())
            row = (
                (
                    await session.execute(
                        _SET_ACTOR,
                        {
                            "email": email,
                            "role": role,
                            "name": name,
                            "is_active": active,
                            "forget": forget,
                        },
                    )
                )
                .mappings()
                .one()
            )
            # Same rule as the admin API. With no active admin to begin with (a new database,
            # or a recovery), the CLI is how the first one is made, so that case stays open.
            if admins_before and not await session.scalar(_COUNT_ACTIVE_ADMINS):
                await session.rollback()
                raise LastAdminError(
                    f"{email} is the last active admin; make someone else admin first."
                )
            await session.commit()
            return row["email"], row["role"], row["is_active"]
    finally:
        await engine.dispose()


def main(argv: Sequence[str] = ()) -> int:
    arguments = build_actor_parser().parse_args(argv)
    email = arguments.email.strip().lower()
    if "@" not in email:
        raise ValidationError(f"{arguments.email!r} is not an email address.")
    stored_email, role, active = asyncio.run(
        persist_actor(
            Settings(),
            email=email,
            role=arguments.role,
            name=arguments.name,
            active=arguments.active,
            forget=arguments.forget,
        )
    )
    print(f"{stored_email}: {role}, {'active' if active else 'deactivated'}")
    return 0
