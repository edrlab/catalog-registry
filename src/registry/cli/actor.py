"""Add someone to the admin API, or change their role or access.

    python -m registry.cli actor set roni@edrlab.org --role admin --name "Roni"
    python -m registry.cli actor set former@edrlab.org --deactivate

The first administrator has to come from somewhere, and the CLI is trusted by holding the database
credential, as `seed` and `add` are. Everyone after that is a viewer on first login until an
administrator changes their role.
"""

import argparse
import asyncio
from collections.abc import Sequence

from sqlalchemy import text

from registry.core.config import Settings
from registry.core.errors import ValidationError
from registry.db.session import build_session_factory, create_database_engine
from registry.domain.enums import ActorRole

#: `role` is applied only when given, so reactivating someone does not reset it.
_SET_ACTOR = text(
    """
INSERT INTO actors (email, name, role, is_active)
VALUES (:email, :name, CAST(coalesce(:role, 'viewer') AS actor_role), :is_active)
ON CONFLICT (email) DO UPDATE
   SET role = CAST(coalesce(:role, actors.role::text) AS actor_role),
       name = coalesce(:name, actors.name),
       is_active = :is_active
RETURNING email, role::text AS role, is_active
"""
)


def build_actor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m registry.cli actor")
    parser.add_argument("action", choices=["set"])
    parser.add_argument("email")
    parser.add_argument("--role", choices=[role.value for role in ActorRole])
    parser.add_argument("--name")
    parser.add_argument("--deactivate", action="store_true", help="remove their access")
    return parser


async def persist_actor(
    settings: Settings, *, email: str, role: str | None, name: str | None, active: bool
) -> tuple[str, str, bool]:
    engine = create_database_engine(settings)
    try:
        async with build_session_factory(engine)() as session:
            row = (
                (
                    await session.execute(
                        _SET_ACTOR,
                        {"email": email, "role": role, "name": name, "is_active": active},
                    )
                )
                .mappings()
                .one()
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
            active=not arguments.deactivate,
        )
    )
    print(f"{stored_email}: {role}, {'active' if active else 'deactivated'}")
    return 0
