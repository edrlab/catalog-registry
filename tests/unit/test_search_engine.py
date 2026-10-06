"""`build_read_engine`: the pool search runs on (ADR-058 amended, ADR-060). No connection made."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from registry.core.config import Settings
from registry.db import session as session_module
from registry.db.session import READ_POOL_RECYCLE_SECONDS, build_read_engine
from registry.repositories import search_repository
from registry.repositories.search_repository import (
    SEARCH_CONNECTION_SETTINGS,
    SEARCH_STATEMENT_TIMEOUT_MS,
    WORD_SIMILARITY_THRESHOLD,
)

pytestmark = pytest.mark.unit

URL = "postgresql+asyncpg://u:p@localhost:5432/registry_test"


def settings() -> Settings:
    return Settings(database_url=URL, environment="test", base_url="http://testserver")


def spy_on_engine_arguments(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The keyword arguments `build_read_engine` hands to SQLAlchemy, while still building the
    real engine. The wire-level effect of `server_settings` is proven against Postgres in
    `test_search_timeout`, where `SHOW` reads them back from a real connection."""
    captured: dict[str, Any] = {}

    def spy(url: str, **kwargs: Any) -> AsyncEngine:
        captured.update(kwargs, url=url)
        return create_async_engine(url, **kwargs)

    monkeypatch.setattr(session_module, "create_async_engine", spy)
    return captured


def test_the_engine_is_autocommit_with_no_pre_ping_and_a_five_minute_recycle() -> None:
    engine = build_read_engine(settings(), {"statement_timeout": "123"})
    pool = engine.sync_engine.pool

    assert engine.sync_engine.dialect._on_connect_isolation_level == "AUTOCOMMIT"
    assert pool._pre_ping is False
    assert pool._recycle == 300
    assert READ_POOL_RECYCLE_SECONDS == 300


def test_the_arguments_given_to_sqlalchemy_are_the_documented_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = spy_on_engine_arguments(monkeypatch)

    build_read_engine(settings(), {"statement_timeout": "150"})

    assert captured["isolation_level"] == "AUTOCOMMIT"
    assert captured["pool_pre_ping"] is False
    assert captured["pool_recycle"] == 300
    assert captured["connect_args"] == {"server_settings": {"statement_timeout": "150"}}
    assert captured["url"] == URL


def test_the_given_server_settings_are_passed_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = spy_on_engine_arguments(monkeypatch)
    given = {"statement_timeout": "150", "default_transaction_read_only": "on"}

    build_read_engine(settings(), given)

    assert captured["connect_args"]["server_settings"] == given


def test_the_settings_are_copied_not_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = spy_on_engine_arguments(monkeypatch)
    given = {"statement_timeout": "150"}

    build_read_engine(settings(), given)
    given["statement_timeout"] = "9999"

    assert captured["connect_args"]["server_settings"] == {"statement_timeout": "150"}


def test_the_production_settings_are_exactly_these_three() -> None:
    assert dict(SEARCH_CONNECTION_SETTINGS) == {
        "statement_timeout": "1000",
        "default_transaction_read_only": "on",
        "pg_trgm.word_similarity_threshold": "0.5",
    }


def test_the_settings_are_built_from_the_repository_constants() -> None:
    assert SEARCH_STATEMENT_TIMEOUT_MS == 1000
    assert WORD_SIMILARITY_THRESHOLD == 0.5
    assert SEARCH_CONNECTION_SETTINGS["statement_timeout"] == str(SEARCH_STATEMENT_TIMEOUT_MS)
    assert SEARCH_CONNECTION_SETTINGS["pg_trgm.word_similarity_threshold"] == str(
        WORD_SIMILARITY_THRESHOLD
    )


def test_the_threshold_the_search_sql_relies_on_is_the_one_sent_to_the_connection() -> None:
    """The statement reads the connection's threshold, so the constant must be the one source."""
    assert search_repository.WORD_SIMILARITY_THRESHOLD is WORD_SIMILARITY_THRESHOLD
    assert "word_similarity_threshold" not in search_repository._SEARCH_SQL
