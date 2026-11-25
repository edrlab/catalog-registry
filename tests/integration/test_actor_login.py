"""A login notes `last_seen_at` at most once a minute, and always answers with the person."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.repositories.actor_repository import ActorRepository

pytestmark = pytest.mark.integration

EMAIL = "login-throttle@edrlab.org"


async def seen_at(session: AsyncSession) -> object:
    return await session.scalar(
        text("SELECT last_seen_at FROM actors WHERE email = :e"), {"e": EMAIL}
    )


async def test_a_login_within_the_minute_writes_nothing_and_still_answers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        store = ActorRepository(session)
        try:
            first = await store.persist_actor_login(
                email=EMAIL, name=None, issuer="https://idp", subject="s1"
            )
            noted = await seen_at(session)
            again = await store.persist_actor_login(
                email=EMAIL, name=None, issuer="https://idp", subject="s1"
            )
            unchanged = await seen_at(session)

            await session.execute(
                text(
                    "UPDATE actors SET last_seen_at = now() - interval '2 minutes' WHERE email = :e"
                ),
                {"e": EMAIL},
            )
            await session.commit()
            stale = await seen_at(session)
            later = await store.persist_actor_login(
                email=EMAIL, name=None, issuer="https://idp", subject="s1"
            )
            renewed = await seen_at(session)
        finally:
            await session.execute(text("DELETE FROM actors WHERE email = :e"), {"e": EMAIL})
            await session.commit()

    assert first.previous_seen_at is None  # a first visit
    assert again.id == first.id and again.email == EMAIL  # within the minute: same person back
    assert unchanged == noted  # ... and no write
    assert again.previous_seen_at == noted
    assert later.previous_seen_at == stale  # past the minute: written again
    assert renewed is not None and stale is not None and renewed > stale
