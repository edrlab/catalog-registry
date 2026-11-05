"""`RetryPolicy` and `parse_retry_after`: retry what may pass soon, never past the budget."""

from datetime import UTC, datetime

import pytest

from registry.network.retry import RETRY_STATUSES, RetryPolicy, parse_retry_after

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


def test_only_busy_statuses_are_retried() -> None:
    assert frozenset({408, 429, 502, 503, 504}) == RETRY_STATUSES
    assert not RETRY_STATUSES & {400, 401, 403, 404, 410, 500, 501}


def test_backoff_doubles_up_to_the_cap_with_bounded_jitter() -> None:
    no_jitter = RetryPolicy(random=lambda: 0.0)
    full_jitter = RetryPolicy(random=lambda: 1.0)

    assert [no_jitter.build_delay(n) for n in (1, 2)] == [0.5, 1.0]
    assert [full_jitter.build_delay(n) for n in (1, 2)] == [0.625, 1.25]


def test_retries_stop_after_the_limit() -> None:
    assert RetryPolicy().build_delay(3) is None
    assert RetryPolicy(retries=0).build_delay(1) is None


@pytest.mark.parametrize(("header", "delay"), [("1", 1.0), ("2", 2.0), ("0", 0.0)])
def test_a_short_retry_after_is_honoured(header: str, delay: float) -> None:
    assert RetryPolicy().build_delay(1, header) == delay


def test_a_long_retry_after_means_no_retry() -> None:
    assert RetryPolicy().build_delay(1, "120") is None


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("3", 3.0),
        ("Thu, 05 Nov 2026 12:00:02 GMT", 2.0),
        ("Thu, 05 Nov 2026 11:00:00 GMT", 0.0),
        ("soon", None),
        ("", None),
        (None, None),
        ("-1", None),
    ],
)
def test_retry_after_reads_seconds_and_http_dates(value: str | None, seconds: float | None) -> None:
    assert parse_retry_after(value, now=NOW) == seconds
