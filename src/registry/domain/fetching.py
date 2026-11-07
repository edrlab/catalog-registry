"""What an outbound fetch returns, and how it fails. Pure data. Stdlib only.

`network/` produces these; services read them through the `FeedFetcher` protocol, so nothing above
`network/` knows which HTTP client is used.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Hop:
    """One redirect followed: where it pointed from, and the status that sent it on."""

    url: str
    status: int


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    #: Lowercased names.
    headers: Mapping[str, str]
    body: bytes
    redirects: tuple[Hop, ...] = ()
    #: The body stopped at the byte limit on purpose (a probe), not because it ended.
    truncated: bool = False
    elapsed_ms: int = 0
    attempts: int = 1

    @property
    def media_type(self) -> str:
        """The `Content-Type` without parameters, lowercased; "" when absent."""
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


@dataclass(slots=True)
class FetchError(Exception):
    """A fetch that produced no usable response. `code` is a finding code (`feed_check.FINDINGS`):
    `url-invalid`, `url-unsafe`, `host-unresolvable`, `fetch-failed`, `fetch-timed-out`,
    `fetch-too-large`, `redirect-unsafe` or `redirect-loop`. `detail` is a fixed sentence that
    never quotes a response body."""

    code: str
    detail: str
    url: str = ""
    redirects: tuple[Hop, ...] = field(default=())

    def __str__(self) -> str:
        return self.detail
