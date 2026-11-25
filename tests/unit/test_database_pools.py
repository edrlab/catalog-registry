"""The three pools `create_app` builds take their size and their TLS from `Settings`.

No database: building an engine connects to nothing, and the one test that opens a connection stops
it at `asyncpg.connect`, where the arguments the driver would get are read.
"""

from typing import Any

import asyncpg
import pytest
from pydantic import ValidationError
from sqlalchemy.pool import QueuePool

from registry.core.config import Settings
from registry.main import create_app

pytestmark = pytest.mark.unit

URL = "postgresql+asyncpg://u:p@db.example:5432/registry_test"
ENGINES = ("engine", "read_engine", "search_engine")


def settings(**changes: Any) -> Settings:
    """Settings from the arguments and the environment only: a developer's `.env` is not read."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        **{"database_url": URL, "environment": "test", "base_url": "http://testserver", **changes},
    )


async def pool_sizes(built: Settings) -> list[tuple[int, int]]:
    """(size, overflow) of the app's three pools, in `ENGINES` order."""
    app = create_app(built)
    async with app.router.lifespan_context(app):
        pools = [getattr(app.state, name).sync_engine.pool for name in ENGINES]
    assert all(isinstance(pool, QueuePool) for pool in pools)
    return [(pool.size(), pool._max_overflow) for pool in pools]


async def test_every_pool_of_the_app_has_the_configured_size_and_overflow() -> None:
    sized = settings(database_pool_size=2, database_max_overflow=1)

    assert await pool_sizes(sized) == [(2, 1)] * len(ENGINES)


@pytest.mark.parametrize(
    "changes",
    [{"database_pool_size": 0}, {"database_pool_size": -1}, {"database_max_overflow": -1}],
)
def test_a_pool_that_could_not_serve_a_request_is_refused_at_boot(changes: dict[str, int]) -> None:
    with pytest.raises(ValidationError) as raised:
        settings(**changes)

    assert [error["loc"] for error in raised.value.errors()] == [(next(iter(changes)),)]


def test_no_overflow_is_allowed() -> None:
    assert settings(database_max_overflow=0).database_max_overflow == 0


async def test_the_pool_size_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGISTRY_DATABASE_POOL_SIZE", "7")
    monkeypatch.setenv("REGISTRY_DATABASE_MAX_OVERFLOW", "3")

    assert await pool_sizes(settings()) == [(7, 3)] * len(ENGINES)


class RefusedError(Exception):
    """Raised by the stand-in for `asyncpg.connect` once it has seen the arguments."""


async def test_ssl_verify_full_in_the_url_reaches_asyncpg_on_every_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`?ssl=verify-full` is the documented way to check the server's certificate; `require`
    would encrypt without checking it. The read pools also send their server settings."""
    seen: list[dict[str, Any]] = []

    async def connect(*_args: Any, **kwargs: Any) -> None:
        seen.append(kwargs)
        raise RefusedError

    monkeypatch.setattr(asyncpg, "connect", connect)
    app = create_app(settings(database_url=f"{URL}?ssl=verify-full"))

    async with app.router.lifespan_context(app):
        for name in ENGINES:
            with pytest.raises(RefusedError):
                async with getattr(app.state, name).connect():
                    pass

    assert [kwargs["ssl"] for kwargs in seen] == ["verify-full"] * 3
    assert [kwargs["host"] for kwargs in seen] == ["db.example"] * 3
    assert "server_settings" not in seen[0]
    assert seen[1]["server_settings"]["statement_timeout"] == "10000"
    assert seen[2]["server_settings"]["statement_timeout"] == "1000"
