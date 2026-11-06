"""When a failed request deserves another attempt, and how long to wait first. Pure.

Retry only what may succeed a moment later: a refused or reset connection, a timeout, and the
statuses that mean "busy" (408, 429, 502, 503, 504). Never a 4xx about the request itself, never an
unsafe URL, never past the caller's deadline.
"""

import email.utils
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

RETRY_STATUSES = frozenset({408, 429, 502, 503, 504})


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """`Retry-After` in seconds (RFC 9110 §10.2.3: delay-seconds or an HTTP date). None if absent
    or unreadable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    return max(0.0, (moment - (now or datetime.now(UTC))).total_seconds())


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    #: Attempts after the first.
    retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 1.0
    #: A server asking for a longer wait than this is not waited for.
    max_retry_after: float = 2.0
    #: Up to this fraction is added at random, so retries from many checks do not line up.
    jitter: float = 0.25
    random: Callable[[], float] = field(default=random.random, compare=False)

    def build_delay(self, retry: int, retry_after: str | None = None) -> float | None:
        """Seconds to wait before retry number *retry* (1-based), or None when not to retry."""
        if retry > self.retries:
            return None
        asked = parse_retry_after(retry_after)
        if asked is not None:
            return asked if asked <= self.max_retry_after else None
        delay = min(self.max_delay, self.base_delay * 2 ** (retry - 1))
        return float(delay * (1 + self.jitter * self.random()))
