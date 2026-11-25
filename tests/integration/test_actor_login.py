"""A login notes `last_seen_at` at most once a minute, records the first identity as a pair, and
always answers with the person.

Real connections and real commits: inside the rollback fixture's one transaction `now()` never
moves and no second connection can wait on a row.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from registry.core.errors import PermissionDeniedError
from registry.domain.admin_models import AdminActor
from registry.repositories.actor_repository import ActorRepository
from registry.services.admin_identity import AdminIdentity
from registry.services.admin_service import resolve_admin_actor
from tests.lock_helpers import wait_until_blocked

pytestmark = pytest.mark.integration

IDP = "https://idp.example.test"


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    built = create_async_engine(migrated_database)
    yield built
    await built.dispose()


@pytest.fixture
async def email(engine: AsyncEngine) -> AsyncIterator[str]:
    address = f"login-{uuid.uuid4().hex[:8]}@edrlab.org"
    yield address
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM actors WHERE email = :e"), {"e": address})


async def log_in(
    engine: AsyncEngine, email: str, issuer: str = IDP, subject: str | None = "sub-1"
) -> AdminActor:
    """The real login path: the repository behind the service's identity check."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        identity = AdminIdentity(email=email, name=None, issuer=issuer, subject=subject)
        return await resolve_admin_actor(ActorRepository(session), identity)


async def stored(engine: AsyncEngine, email: str) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT ctid::text AS ctid, last_seen_at, identity_issuer, identity_subject "
                    "FROM actors WHERE email = :e"
                ),
                {"e": email},
            )
        ).one()


async def age(engine: AsyncEngine, email: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE actors SET last_seen_at = now() - interval '2 minutes' WHERE email = :e"),
            {"e": email},
        )


async def test_a_login_within_the_minute_writes_nothing_and_still_answers(
    engine: AsyncEngine, email: str
) -> None:
    first = await log_in(engine, email)
    noted = await stored(engine, email)
    again = await log_in(engine, email)
    unchanged = await stored(engine, email)
    await age(engine, email)
    stale = await stored(engine, email)
    later = await log_in(engine, email)
    renewed = await stored(engine, email)

    assert first.previous_seen_at is None  # a first visit
    assert (again.id, again.email) == (first.id, email)  # within the minute: same person back
    assert (unchanged.ctid, unchanged.last_seen_at) == (noted.ctid, noted.last_seen_at)  # no write
    assert again.previous_seen_at == noted.last_seen_at
    assert later.previous_seen_at == stale.last_seen_at  # past the minute: written again
    assert renewed.ctid != stale.ctid
    assert renewed.last_seen_at > stale.last_seen_at


async def test_two_first_logins_at_once_both_answer(engine: AsyncEngine, email: str) -> None:
    """The first login's row is committed while the second waits on it: the second login's
    snapshot predates the row, and it must still answer with the person, not fail."""
    async with engine.connect() as first, engine.connect() as observer:
        blocker = int(await first.scalar(text("SELECT pg_backend_pid()")) or 0)
        inserted = await first.scalar(
            text(
                "INSERT INTO actors (email, identity_issuer, identity_subject, last_seen_at) "
                "VALUES (:e, :i, 'sub-1', now()) RETURNING id::text"
            ),
            {"e": email, "i": IDP},
        )
        second = asyncio.create_task(log_in(engine, email))
        blocked = await wait_until_blocked(observer, None, blocker)
        await first.commit()
        actor = await asyncio.wait_for(second, timeout=10)

    assert blocked, "the second login never waited on the first"
    assert actor.id == inserted


async def test_another_account_arriving_with_the_first_login_is_refused(
    engine: AsyncEngine, email: str
) -> None:
    """A second account with the same email, waiting on the first login's row: it must be refused
    against the identity that row stores, and must not replace it."""
    async with engine.connect() as first, engine.connect() as observer:
        blocker = int(await first.scalar(text("SELECT pg_backend_pid()")) or 0)
        await first.execute(
            text(
                "INSERT INTO actors (email, identity_issuer, identity_subject, last_seen_at) "
                "VALUES (:e, :i, 'sub-1', now())"
            ),
            {"e": email, "i": IDP},
        )
        second = asyncio.create_task(log_in(engine, email, subject="sub-2"))
        blocked = await wait_until_blocked(observer, None, blocker)
        await first.commit()
        with pytest.raises(PermissionDeniedError):
            await asyncio.wait_for(second, timeout=10)
    row = await stored(engine, email)

    assert blocked, "the second login never waited on the first"
    assert (row.identity_issuer, row.identity_subject) == (IDP, "sub-1")


async def test_a_deactivation_committed_while_a_login_waits_is_seen(
    engine: AsyncEngine, email: str
) -> None:
    """The login notes nothing (seen within the minute) and must answer from the row as it is now,
    not from the snapshot it started with: a person deactivated meanwhile is refused."""
    await log_in(engine, email)
    async with engine.connect() as admin, engine.connect() as observer:
        blocker = int(await admin.scalar(text("SELECT pg_backend_pid()")) or 0)
        await admin.execute(
            text("UPDATE actors SET is_active = false WHERE email = :e"), {"e": email}
        )
        login = asyncio.create_task(log_in(engine, email))
        blocked = await wait_until_blocked(observer, None, blocker)
        await admin.commit()
        with pytest.raises(PermissionDeniedError):
            await asyncio.wait_for(login, timeout=10)

    assert blocked, "the login never waited on the deactivation"


async def test_a_first_identity_is_recorded_even_within_the_minute(
    engine: AsyncEngine, email: str
) -> None:
    await log_in(engine, email, subject=None)
    before = await stored(engine, email)
    actor = await log_in(engine, email)
    after = await stored(engine, email)

    assert (before.identity_issuer, before.identity_subject) == (IDP, None)
    assert (after.identity_issuer, after.identity_subject) == (IDP, "sub-1")
    assert (actor.identity_issuer, actor.identity_subject) == (IDP, "sub-1")


async def test_a_first_subject_from_a_new_issuer_records_both(
    engine: AsyncEngine, email: str
) -> None:
    """A dev login (no subject) first: the account that brings the first subject is recorded
    whole, so the same login again is that account and not a stranger."""
    await log_in(engine, email, issuer="dev", subject=None)
    await log_in(engine, email)
    again = await log_in(engine, email)
    row = await stored(engine, email)

    assert (row.identity_issuer, row.identity_subject) == (IDP, "sub-1")
    assert (again.identity_issuer, again.identity_subject) == (IDP, "sub-1")


async def test_a_refused_login_writes_nothing(engine: AsyncEngine, email: str) -> None:
    await log_in(engine, email)
    await age(engine, email)
    before = await stored(engine, email)

    with pytest.raises(PermissionDeniedError):
        await log_in(engine, email, subject="sub-2")
    after = await stored(engine, email)

    assert (after.ctid, after.last_seen_at) == (before.ctid, before.last_seen_at)
    assert (after.identity_issuer, after.identity_subject) == (IDP, "sub-1")
