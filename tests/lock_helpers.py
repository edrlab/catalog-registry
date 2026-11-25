"""Watching one connection wait on another's lock, for tests that really commit."""

import asyncio
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

OBSERVE_FOR_SECONDS = 5.0

#: Backends waiting on *b*, directly or behind another waiter (a second row-lock waiter queues on
#: the first one's tuple lock). `pg_locks`, not `pg_stat_activity`: the activity view is a snapshot
#: taken once per transaction, so a backend that connects after the first poll would never appear.
_WAITING_ON = text(
    """
WITH RECURSIVE behind(pid) AS (
  SELECT pid FROM pg_locks WHERE NOT granted AND CAST(:b AS int) = ANY (pg_blocking_pids(pid))
  UNION
  SELECT l.pid FROM pg_locks l JOIN behind w ON w.pid = ANY (pg_blocking_pids(l.pid))
   WHERE NOT l.granted
)
SELECT count(*) FROM behind WHERE CAST(:v AS int) IS NULL OR pid = CAST(:v AS int)
"""
)


async def wait_until_blocked(
    observer: AsyncConnection, victim: int | None, blocker: int, *, waiters: int = 1
) -> bool:
    """Poll until *victim* waits on *blocker* (or, when *victim* is None, until *waiters* backends
    do), or give up after `OBSERVE_FOR_SECONDS`."""
    deadline = time.monotonic() + OBSERVE_FOR_SECONDS
    while time.monotonic() < deadline:
        if (await observer.scalar(_WAITING_ON, {"v": victim, "b": blocker}) or 0) >= waiters:
            return True
        await asyncio.sleep(0.02)
    return False
