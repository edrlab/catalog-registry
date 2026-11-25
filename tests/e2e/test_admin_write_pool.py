"""The admin API's writes on the app's own write pool: a write stuck behind a lock fails fast.

Not rebound to the rollback fixture: the limits ride on the connections `create_app` builds. The
production lock timeout is 5 s, so the pool is built with a short one; rows are really committed
and deleted in a `finally`.
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from registry.core.config import Settings
from registry.core.constants import PROBLEM_JSON_MEDIA_TYPE
from registry.db import session as db_session_module
from registry.main import create_app
from tests.conftest import ADMIN_SERVICE_KEY, admin_headers
from tests.read_helpers import checked_out, link_document
from tests.search_helpers import committed_catalogs

pytestmark = pytest.mark.e2e

EDITOR = "write-pool-editor@edrlab.org"
LOCK_TIMEOUT_MS = 200


@pytest.fixture
async def write_app(
    settings: Settings, migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FastAPI]:
    monkeypatch.setitem(
        db_session_module.WRITE_CONNECTION_SETTINGS, "lock_timeout", str(LOCK_TIMEOUT_MS)
    )
    local = settings.model_copy(
        update={
            "environment": "local",
            "service_keys": SecretStr(ADMIN_SERVICE_KEY),
            "admin_dev_identity": True,
            "admin_token_issuer": None,
            "admin_token_audience": None,
            "admin_jwks_url": None,
        }
    )
    built = create_app(local)
    async with built.router.lifespan_context(built):
        async with built.state.engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO actors (email, role) VALUES (:e, 'editor')"), {"e": EDITOR}
            )
        try:
            yield built
        finally:
            async with built.state.engine.begin() as connection:
                await connection.execute(text("DELETE FROM actors WHERE email = :e"), {"e": EDITOR})


async def test_a_write_blocked_by_a_lock_fails_fast_and_the_pool_recovers(
    write_app: FastAPI, migrated_database: str
) -> None:
    locker = create_async_engine(migrated_database)
    transport = ASGITransport(app=write_app)
    async with (
        committed_catalogs(migrated_database, [link_document("Locked shelf")]) as [catalog_id],
        AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        path = f"/admin/catalogs/{catalog_id}"
        try:
            async with locker.connect() as connection:
                await connection.execute(
                    text("SELECT 1 FROM catalogs WHERE id = :c FOR UPDATE"), {"c": catalog_id}
                )
                started = time.monotonic()
                blocked = await asyncio.wait_for(
                    client.patch(
                        path, json={"version": 1, "name": "Renamed"}, headers=admin_headers(EDITOR)
                    ),
                    timeout=5,
                )
                waited = time.monotonic() - started
                await connection.rollback()
            released = await client.patch(
                path, json={"version": 1, "name": "Renamed"}, headers=admin_headers(EDITOR)
            )
        finally:
            await locker.dispose()

    assert blocked.status_code == 503, blocked.text
    assert blocked.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert blocked.json()["detail"] == "The change waited too long for another one; try again."
    # Its own type and a retry hint, so a client never takes it for `unavailable` (not set up).
    assert blocked.json()["type"] == "https://registry.thoriumreader.com/problems/write-timeout"
    assert blocked.headers["retry-after"] == "2"
    assert LOCK_TIMEOUT_MS / 1000 <= waited < 2
    assert released.status_code == 200, released.text
    assert released.json()["name"] == "Renamed"
    assert checked_out(write_app.state.engine) == 0
