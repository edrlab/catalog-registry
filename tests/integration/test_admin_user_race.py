"""Two admins demoting each other at the same moment leave exactly one admin.

Real connections and real commits, not the test transaction: the point is the row locks.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from registry.core.errors import LastAdminError, PermissionDeniedError
from registry.domain.admin_models import AdminActor
from registry.domain.enums import ActorRole
from registry.repositories.actor_repository import ActorRepository
from registry.services.user_service import apply_user_role_change

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    built = create_async_engine(migrated_database)
    yield built
    await built.dispose()


async def test_mutual_demotion_leaves_one_admin(engine: AsyncEngine) -> None:
    tag = uuid.uuid4().hex[:8]
    emails = [f"race-a-{tag}@edrlab.org", f"race-b-{tag}@edrlab.org"]
    async with engine.begin() as connection:
        ids = [
            (
                await connection.execute(
                    text(
                        "INSERT INTO actors (email, role) VALUES (:e, 'admin') RETURNING id::text"
                    ),
                    {"e": email},
                )
            ).scalar_one()
            for email in emails
        ]
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actors = [
        AdminActor(id=ids[n], email=emails[n], name=None, role=ActorRole.ADMIN, is_active=True)
        for n in range(2)
    ]

    async def demote(caller: int) -> object:
        async with sessions() as session:
            return await apply_user_role_change(
                ActorRepository(session), actors[caller], ids[1 - caller], ActorRole.VIEWER
            )

    try:
        outcomes = await asyncio.gather(demote(0), demote(1), return_exceptions=True)
        async with engine.connect() as connection:
            admins = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM actors WHERE role = 'admin' AND is_active "
                        "AND email = ANY(:e)"
                    ),
                    {"e": emails},
                )
            ).scalar_one()
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM actors WHERE email = ANY(:e)"), {"e": emails}
            )

    assert admins == 1
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], PermissionDeniedError | LastAdminError)
