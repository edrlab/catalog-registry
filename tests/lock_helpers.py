"""Watching one connection wait on another's lock, for tests that really commit."""

import asyncio
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

OBSERVE_FOR_SECONDS = 5.0

#: `pg_locks`, not `pg_stat_activity`: the activity view is a snapshot taken once per transaction,
#: so a backend that connects after the observer's first poll would never appear in it.
_WAITING_ON = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted "
    "AND CAST(:b AS int) = ANY (pg_blocking_pids(pid)) "
    "AND (CAST(:v AS int) IS NULL OR pid = CAST(:v AS int)))"
)


async def wait_until_blocked(observer: AsyncConnection, victim: int | None, blocker: int) -> bool:
    """Poll `pg_blocking_pids` until *victim* (any backend when None) waits on *blocker*, or give
    up after `OBSERVE_FOR_SECONDS`."""
    deadline = time.monotonic() + OBSERVE_FOR_SECONDS
    while time.monotonic() < deadline:
        if await observer.scalar(_WAITING_ON, {"v": victim, "b": blocker}):
            return True
        await asyncio.sleep(0.02)
    return False
