"""`python -m registry.cli actor set`: the way the first administrator exists."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from registry.cli.actor import main, persist_actor
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
        created = await persist_actor(settings, email=EMAIL, role="admin", name="Cli", active=True)
        renamed = await persist_actor(settings, email=EMAIL, role=None, name=None, active=False)
        restored = await persist_actor(settings, email=EMAIL, role=None, name=None, active=True)
        demoted = await persist_actor(settings, email=EMAIL, role="viewer", name=None, active=True)

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

    assert created == (EMAIL, "admin", True)
    assert renamed == (EMAIL, "admin", False)  # deactivating keeps the role
    assert restored == (EMAIL, "admin", True)
    assert demoted == (EMAIL, "viewer", True)
    assert stored == "Cli"  # a later call without a name keeps it


def test_an_address_without_an_at_sign_is_refused() -> None:
    with pytest.raises(ValidationError):
        main(["set", "not-an-email"])
