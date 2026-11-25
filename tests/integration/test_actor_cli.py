"""`python -m registry.cli actor set`: the way the first administrator exists."""

import asyncio
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.cli import actor as actor_module
from registry.cli.actor import build_actor_parser, main, persist_actor
from registry.core.config import Settings
from registry.core.errors import LastAdminError, PermissionDeniedError, ValidationError
from registry.domain.admin_models import AdminActor
from registry.domain.enums import ActorRole
from registry.repositories.actor_repository import ActorRepository
from registry.services.user_service import apply_user_role_change
from tests.lock_helpers import wait_until_blocked

pytestmark = pytest.mark.integration

EMAIL = "cli-actor@edrlab.org"
OTHER = "cli-other-admin@edrlab.org"


async def test_set_creates_changes_and_deactivates_without_losing_the_role(
    settings: Settings,
    migrated_database: str,
) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        # Another active admin, so taking this one's access is not removing the last admin.
        await persist_actor(settings, email=OTHER, role="admin", name=None, active=None)
        created = await persist_actor(settings, email=EMAIL, role="admin", name="Cli", active=None)
        renamed = await persist_actor(settings, email=EMAIL, role=None, name=None, active=False)
        demoted = await persist_actor(settings, email=EMAIL, role="viewer", name=None, active=None)
        restored = await persist_actor(settings, email=EMAIL, role=None, name=None, active=True)

        async with engine.connect() as connection:
            stored = (
                await connection.execute(
                    text("SELECT name FROM actors WHERE email = :e"), {"e": EMAIL}
                )
            ).scalar_one()
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM actors WHERE email = ANY(:e)"), {"e": [EMAIL, OTHER]}
            )
        await engine.dispose()

    assert created == (EMAIL, "admin", True)  # a new person is active
    assert renamed == (EMAIL, "admin", False)  # deactivating keeps the role
    assert demoted == (EMAIL, "viewer", False)  # changing the role does not reactivate
    assert restored == (EMAIL, "viewer", True)  # only an explicit activation does
    assert stored == "Cli"  # a later call without a name keeps it


def test_activate_and_deactivate_cannot_be_combined() -> None:
    with pytest.raises(SystemExit):
        build_actor_parser().parse_args(["set", EMAIL, "--activate", "--deactivate"])


def test_neither_flag_leaves_the_access_unchanged() -> None:
    assert build_actor_parser().parse_args(["set", EMAIL, "--role", "viewer"]).active is None


def test_an_address_without_an_at_sign_is_refused() -> None:
    with pytest.raises(ValidationError):
        main(["set", "not-an-email"])


def test_the_command_stores_the_address_lowercased_and_reports_the_result(
    settings: Settings,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The email is the actor's key, so it is stored as the sign-in will present it."""
    monkeypatch.setattr(actor_module, "Settings", lambda: settings)
    try:
        status = main(["set", "  CLI-Main@EDRLab.org ", "--role", "admin", "--deactivate"])
    finally:
        asyncio.run(delete_actor(settings, "cli-main@edrlab.org"))

    assert (status, capsys.readouterr().out) == (0, "cli-main@edrlab.org: admin, deactivated\n")


async def delete_actor(settings: Settings, email: str) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM actors WHERE email = :e"), {"e": email})
    finally:
        await engine.dispose()


async def test_the_last_active_admin_cannot_be_demoted_or_deactivated_but_the_first_can_be_made(
    settings: Settings,
    migrated_database: str,
) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            assert not await connection.scalar(
                text("SELECT count(*) FROM actors WHERE role = 'admin' AND is_active")
            ), "this test needs a database with no active admin"
        first = await persist_actor(settings, email=EMAIL, role="admin", name=None, active=None)
        with pytest.raises(LastAdminError):
            await persist_actor(settings, email=EMAIL, role="viewer", name=None, active=None)
        with pytest.raises(LastAdminError):
            await persist_actor(settings, email=EMAIL, role=None, name=None, active=False)
        async with engine.connect() as connection:
            kept = (
                await connection.execute(
                    text("SELECT role::text, is_active FROM actors WHERE email = :e"), {"e": EMAIL}
                )
            ).one()
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM actors WHERE email = :e"), {"e": EMAIL})
        await engine.dispose()

    assert first == (EMAIL, "admin", True)  # with no admin at all, the CLI makes the first one
    assert tuple(kept) == ("admin", True)  # both refusals rolled back


async def test_forget_identity_lets_the_next_login_record_a_new_account(
    settings: Settings,
    migrated_database: str,
) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO actors (email, identity_issuer, identity_subject) "
                    "VALUES (:e, 'https://idp', 'sub-1')"
                ),
                {"e": EMAIL},
            )
        await persist_actor(settings, email=EMAIL, role=None, name=None, active=None, forget=True)
        async with engine.connect() as connection:
            identity = (
                await connection.execute(
                    text("SELECT identity_issuer, identity_subject FROM actors WHERE email = :e"),
                    {"e": EMAIL},
                )
            ).one()
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM actors WHERE email = :e"), {"e": EMAIL})
        await engine.dispose()

    assert tuple(identity) == (None, None)


def test_forget_identity_is_off_unless_asked() -> None:
    parser = build_actor_parser()

    assert parser.parse_args(["set", EMAIL, "--forget-identity"]).forget is True
    assert parser.parse_args(["set", EMAIL]).forget is False


def test_the_command_forgets_the_identity_when_asked(
    settings: Settings, migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(actor_module, "Settings", lambda: settings)

    async def stored_identity() -> tuple[object, object]:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT identity_issuer, identity_subject FROM actors WHERE email = :e"
                        ),
                        {"e": EMAIL},
                    )
                ).one()
                return row[0], row[1]
        finally:
            await engine.dispose()

    async def insert_with_identity() -> None:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO actors (email, identity_issuer, identity_subject) "
                        "VALUES (:e, 'https://idp', 'sub-1')"
                    ),
                    {"e": EMAIL},
                )
        finally:
            await engine.dispose()

    asyncio.run(insert_with_identity())
    try:
        kept_status = main(["set", EMAIL])
        kept = asyncio.run(stored_identity())
        forgot_status = main(["set", EMAIL, "--forget-identity"])
        forgotten = asyncio.run(stored_identity())
    finally:
        asyncio.run(delete_actor(settings, EMAIL))

    assert (kept_status, kept) == (0, ("https://idp", "sub-1"))
    assert (forgot_status, forgotten) == (0, (None, None))


def test_demoting_the_last_admin_from_the_command_line_is_status_1_and_says_why(
    settings: Settings,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from registry.cli import __main__ as cli  # noqa: PLC0415

    monkeypatch.setattr(actor_module, "Settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["registry.cli", "actor", "set", EMAIL, "--role", "admin"])
    try:
        made = cli.main()
        capsys.readouterr()
        monkeypatch.setattr(
            sys, "argv", ["registry.cli", "actor", "set", EMAIL, "--role", "viewer"]
        )
        refused = cli.main()
        output = capsys.readouterr()
    finally:
        asyncio.run(delete_actor(settings, EMAIL))

    assert (made, refused) == (0, 1)
    assert output.out == ""
    assert output.err == f"{EMAIL} is the last active admin; make someone else admin first.\n"


async def test_the_command_line_and_the_admin_api_demoting_each_other_leave_one_admin(
    settings: Settings, migrated_database: str
) -> None:
    """A holds A's and B's rows; the API (B demoting A) and the CLI (demoting B) both queue behind
    it, then race. Both lock the admins in the same order, so the second sees the first's change."""
    engine = create_async_engine(settings.database_url)
    emails = [EMAIL, OTHER]
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
    caller = AdminActor(id=ids[1], email=OTHER, name=None, role=ActorRole.ADMIN, is_active=True)

    async def from_the_api() -> object:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return await apply_user_role_change(
                ActorRepository(session), caller, ids[0], ActorRole.VIEWER
            )

    try:
        async with engine.connect() as holder, engine.connect() as observer:
            blocker = int(await holder.scalar(text("SELECT pg_backend_pid()")) or 0)
            await holder.execute(
                text("SELECT 1 FROM actors WHERE email = ANY(:e) ORDER BY id FOR UPDATE"),
                {"e": emails},
            )
            api = asyncio.create_task(from_the_api())
            cli = asyncio.create_task(
                persist_actor(settings, email=OTHER, role="viewer", name=None, active=None)
            )
            both_waiting = await wait_until_blocked(observer, None, blocker, waiters=2)
            await holder.rollback()
            outcomes = await asyncio.wait_for(
                asyncio.gather(api, cli, return_exceptions=True), timeout=10
            )
        async with engine.connect() as connection:
            admins = await connection.scalar(
                text("SELECT count(*) FROM actors WHERE role = 'admin' AND is_active")
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM actors WHERE email = ANY(:e)"), {"e": emails}
            )
        await engine.dispose()

    assert both_waiting, "the API and the CLI did not both wait on the held rows"
    assert admins == 1
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], LastAdminError | PermissionDeniedError)
