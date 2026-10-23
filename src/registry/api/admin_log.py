"""What is logged when someone is refused at `/admin`.

One JSON line per refusal on stdout, which Cloud Run turns into a log entry. It says why (the fixed
problem detail), what was asked (method and path, never the query string) and who asked in the only
terms a caller is known by here: the user agent and the request id. No key, token or address is
ever logged, which is also what `log_client_request` does.

A client that is wrong keeps being wrong: a misconfigured caller retrying, or a probe. Repeating
the line for each request buries everything else, so an identical refusal (same status, reason and
user agent) is logged once a minute, with a count of the ones skipped since the last line.
"""

import json
import logging
import sys
import time
from typing import Final

from fastapi import Request

REFUSAL_LOG_WINDOW_SECONDS: Final = 60.0
SERVER_ERROR_FROM: Final = 500
#: A bound on memory: a probe varying its user agent cannot grow the table without limit.
MAX_TRACKED_REFUSALS: Final = 1000
MAX_USER_AGENT_LENGTH: Final = 120

_log = logging.getLogger("registry.admin")
#: (status, reason, user agent) -> (when it was last logged, how many were skipped since)
_recent: dict[tuple[int, str, str], tuple[float, int]] = {}


def configure_admin_log() -> None:
    if _log.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False


def log_admin_refusal(request: Request, *, status: int, reason: str) -> None:
    """Log one refused `/admin` request, unless an identical one was logged a moment ago."""
    user_agent = request.headers.get("user-agent", "-")[:MAX_USER_AGENT_LENGTH]
    key = (status, reason, user_agent)
    now = time.monotonic()
    previous = _recent.get(key)
    if previous and now - previous[0] < REFUSAL_LOG_WINDOW_SECONDS:
        _recent[key] = (previous[0], previous[1] + 1)
        return
    if len(_recent) >= MAX_TRACKED_REFUSALS:
        _recent.clear()
    _recent[key] = (now, 0)
    state = request.scope.get("state", {})
    failing = status >= SERVER_ERROR_FROM
    line = {
        "severity": "ERROR" if failing else "WARNING",
        "message": "admin request refused",
        "status": status,
        "reason": reason,
        "method": request.method,
        "path": request.url.path,
        "request_id": state.get("request_id", "-"),
        "user_agent": user_agent,
        "skipped_since_last_line": previous[1] if previous else 0,
    }
    _log.log(logging.ERROR if failing else logging.WARNING, json.dumps(line))
