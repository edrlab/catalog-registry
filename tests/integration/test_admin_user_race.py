"""Two admins demoting each other at the same moment leave exactly one admin.

Real connections and real commits, not the test transaction: the point is the row locks. The
overlap is forced, not hoped for: the first change holds its locks until the second is seen
waiting on them (`pg_blocking_pids`), so without the lock the test fails on that wait.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from registry.core.errors import LastAdminError, PermissionDeniedError
from registry.domain.admin_models import AdminActor, AdminUser
from registry.domain.enums import ActorRole
from registry.repositories.actor_repository import ActorRepository
from registry.services.user_service import apply_user_role_change
from tests.integration.test_search_concurrency import OBSERVE_FOR_SECONDS, wait_until_blocked

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
    first_holds_its_locks = asyncio.Event()
    release_first = asyncio.Event()
    pids: dict[int, int] = {}

    class HeldStore(ActorRepository):
        """The first change stops after taking its locks, until the second is seen waiting."""

        async def lock_admin_user(self, user_id: str) -> AdminUser | None:
            user = await super().lock_admin_user(user_id)
            first_holds_its_locks.set()
            await release_first.wait()
            return user

    async def demote(caller: int) -> object:
        async with sessions() as session:
            pids[caller] = int(await session.scalar(text("SELECT pg_backend_pid()")) or 0)
            store = HeldStore(session) if caller == 0 else ActorRepository(session)
            return await apply_user_role_change(
                store, actors[caller], ids[1 - caller], ActorRole.VIEWER
            )

    try:
        first = asyncio.create_task(demote(0))
        await asyncio.wait_for(first_holds_its_locks.wait(), OBSERVE_FOR_SECONDS)
        second = asyncio.create_task(demote(1))
        async with engine.connect() as observer:
            while 1 not in pids and not second.done():
                await asyncio.sleep(0.01)
            # The second change must queue behind the first one's lock on the admins.
            overlapped = await wait_until_blocked(observer, pids[1], pids[0])
        release_first.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
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
        release_first.set()
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM actors WHERE email = ANY(:e)"), {"e": emails}
            )

    assert overlapped
    assert admins == 1
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], PermissionDeniedError | LastAdminError)
