"""A refused `/admin` caller is logged once a minute per kind, and never with a secret."""

import json
import logging
import time
from collections.abc import Iterator

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from registry.api import admin_log, compression

pytestmark = pytest.mark.unit


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(record.getMessage()))


@pytest.fixture
def lines() -> Iterator[Lines]:
    handler = Lines()
    admin_log._log.addHandler(handler)
    admin_log._log.setLevel(logging.INFO)
    admin_log._recent.clear()
    yield handler
    admin_log._log.removeHandler(handler)
    admin_log._recent.clear()


def request(agent: str = "node", path: str = "/admin/me") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"service_key=SECRET",
            "headers": [
                (b"user-agent", agent.encode()),
                (b"x-registry-service-key", b"SECRET"),
                (b"cf-access-jwt-assertion", b"SECRET"),
            ],
            "state": {"request_id": "r-1"},
        }
    )


def test_a_refusal_says_why_what_and_who_and_carries_no_secret(lines: Lines) -> None:
    admin_log.log_admin_refusal(request(), status=401, reason="Missing or invalid service key.")

    [line] = lines.lines
    assert line == {
        "severity": "WARNING",
        "message": "admin request refused",
        "status": 401,
        "reason": "Missing or invalid service key.",
        "method": "GET",
        "path": "/admin/me",
        "request_id": "r-1",
        "user_agent": "node",
        "skipped_since_last_line": 0,
    }
    assert "SECRET" not in json.dumps(line)


def test_an_unavailable_api_is_an_error_not_a_warning(lines: Lines) -> None:
    admin_log.log_admin_refusal(request(), status=503, reason="not configured")

    assert lines.lines[0]["severity"] == "ERROR"


def test_the_same_refusal_is_logged_once_and_the_rest_are_counted(
    lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    for _ in range(5):
        admin_log.log_admin_refusal(request(), status=503, reason="down")
    assert len(lines.lines) == 1

    now[0] += admin_log.REFUSAL_LOG_WINDOW_SECONDS + 1
    admin_log.log_admin_refusal(request(), status=503, reason="down")
    assert [line["skipped_since_last_line"] for line in lines.lines] == [0, 4]


def test_a_different_reason_or_client_is_logged_on_its_own(lines: Lines) -> None:
    admin_log.log_admin_refusal(request(), status=401, reason="a")
    admin_log.log_admin_refusal(request(), status=401, reason="b")
    admin_log.log_admin_refusal(request("curl"), status=401, reason="a")

    assert len(lines.lines) == 3


def test_a_probe_varying_its_user_agent_cannot_grow_the_table_without_limit(
    lines: Lines,
) -> None:
    for number in range(admin_log.MAX_TRACKED_REFUSALS + 10):
        admin_log.log_admin_refusal(request(f"agent-{number}"), status=401, reason="x")

    assert len(admin_log._recent) <= admin_log.MAX_TRACKED_REFUSALS


def test_admin_requests_stay_out_of_the_client_count() -> None:
    handler = Lines()
    compression._client_log.addHandler(handler)
    compression._client_log.setLevel(logging.INFO)
    try:
        for path in ("/admin/me", "/admin", "/search"):
            compression.log_client_request(
                {"type": "http", "path": path}, Headers({}), status=200, encoding=None
            )
    finally:
        compression._client_log.removeHandler(handler)

    assert [line["path"] for line in handler.lines] == ["/search"]
