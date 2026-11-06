"""`python -m registry.cli actor set`: the way the first administrator exists."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from registry.cli.actor import build_actor_parser, main, persist_actor
from registry.core.config import Settings
from registry.core.errors import ValidationError

pytestmark = pytest.mark.integration

EMAIL = "cli-actor@edrlab.org"


async def test_set_creates_changes_and_deactivates_without_losing_the_role(
    settings: Settings,
    migrated_database: str,
) -> None:
    engine = create_async_engine(settings.database_url)
    try:
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
            await connection.execute(text("DELETE FROM actors WHERE email = :e"), {"e": EMAIL})
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
