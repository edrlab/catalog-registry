"""`make add`'s real fetch, over real sockets: `download_feed_document` and the redirect handler.

The rest of the `add` tests stub the download out, so what the command actually puts on the
wire and does with what comes back was never exercised: the `Accept` header, the 2 MB cap, the
timeout, redirects, and every way a hostile or broken origin can answer. This starts a throwaway
HTTP server on loopback and fetches from it. Nothing leaves the machine.

The public-address guard refuses loopback, which is the point of it, so the tests that need to
reach this server patch it out; one test leaves it in and shows it stops the request before
anything is sent.
"""

import contextlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from registry.cli import add as add_module
from registry.cli.add import MAX_FEED_BYTES, download_feed_document, main
from registry.core.errors import ValidationError

pytestmark = pytest.mark.integration

FEED = {"metadata": {"title": "Local Test Library"}, "links": [{"href": "https://x.example/s"}]}

#: The ceiling that would have to be exceeded by one byte; built once, it is 2 MB.
EXACTLY_THE_LIMIT = json.dumps(FEED).encode() + b" " * (
    MAX_FEED_BYTES - len(json.dumps(FEED).encode())
)


class Origin:
    """A throwaway origin server that records every request it receives."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return None

            def _send(
                self, status: int, body: bytes, content_type: str = "application/json"
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                origin.requests.append((self.path, dict(self.headers)))
                routes: dict[str, Callable[[], None]] = {
                    "/ok": lambda: self._send(200, json.dumps(FEED).encode()),
                    "/limit": lambda: self._send(200, EXACTLY_THE_LIMIT),
                    "/over": lambda: self._send(200, EXACTLY_THE_LIMIT + b" "),
                    "/html": lambda: self._send(200, b"<html>no</html>", "text/html"),
                    "/list": lambda: self._send(200, b"[1, 2, 3]"),
                    "/null": lambda: self._send(200, b"null"),
                    "/empty": lambda: self._send(200, b""),
                    "/404": lambda: self._send(404, b"{}"),
                    "/500": lambda: self._send(500, b"{}"),
                    "/slow": self._slow,
                    "/redirect": lambda: self._redirect("/ok"),
                    "/redirect-twice": lambda: self._redirect("/redirect"),
                    "/redirect-ftp": lambda: self._redirect("ftp://files.example/feed"),
                    "/redirect-private": lambda: self._redirect("http://private.invalid/feed"),
                    "/redirect-file": lambda: self._redirect("file:///etc/passwd"),
                }
                routes.get(self.path, lambda: self._send(404, b"{}"))()

            def _slow(self) -> None:
                time.sleep(1.0)
                with contextlib.suppress(OSError):  # the client gave up first, which is the point
                    self._send(200, json.dumps(FEED).encode())

            def _redirect(self, location: str) -> None:
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler


@pytest.fixture
def origin() -> Iterator[Origin]:
    server = Origin()
    server.thread.start()
    try:
        yield server
    finally:
        server.server.shutdown()
        server.server.server_close()


@pytest.fixture
def reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let loopback through the public-address guard, but still refuse `private.invalid`, so a
    redirect to a 'private' host can be shown to be re-checked on its own hop."""
    real = add_module.assert_publicly_reachable

    def guard(url: str) -> None:
        if "private.invalid" in url:
            raise ValidationError(f"{url} resolves to 10.0.0.5, which is not a public address")
        if not url.startswith("http://127.0.0.1"):
            real(url)

    monkeypatch.setattr(add_module, "assert_publicly_reachable", guard)


# --- what goes on the wire -----------------------------------------------------------------


def test_a_feed_is_fetched_and_parsed(origin: Origin, reachable: None) -> None:
    assert download_feed_document(f"{origin.base}/ok") == FEED


def test_the_request_asks_for_opds_json(origin: Origin, reachable: None) -> None:
    download_feed_document(f"{origin.base}/ok")

    ((path, headers),) = origin.requests
    assert path == "/ok"
    assert headers["Accept"] == "application/opds+json, application/json"


# --- the size cap --------------------------------------------------------------------------


def test_a_body_of_exactly_the_limit_is_accepted(origin: Origin, reachable: None) -> None:
    assert len(EXACTLY_THE_LIMIT) == MAX_FEED_BYTES
    assert download_feed_document(f"{origin.base}/limit") == FEED


def test_one_byte_over_the_limit_is_refused(origin: Origin, reachable: None) -> None:
    with pytest.raises(ValidationError, match="more than 2097152 bytes"):
        download_feed_document(f"{origin.base}/over")


# --- what a broken or hostile origin can answer --------------------------------------------


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/html", "did not return JSON"),
        ("/empty", "did not return JSON"),
        ("/list", "returned list, not an OPDS document"),
        ("/null", "returned NoneType, not an OPDS document"),
        ("/404", "could not fetch"),
        ("/500", "could not fetch"),
    ],
)
def test_an_unusable_answer_is_a_validation_error_not_a_crash(
    origin: Origin, reachable: None, path: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        download_feed_document(f"{origin.base}{path}")


def test_a_closed_port_is_a_fetch_error(reachable: None) -> None:
    with pytest.raises(ValidationError, match="could not fetch"):
        download_feed_document("http://127.0.0.1:9/feed")


def test_a_slow_origin_is_cut_off_by_the_timeout(
    origin: Origin, reachable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(add_module, "FETCH_TIMEOUT_SECONDS", 0.3)
    started = time.perf_counter()

    with pytest.raises(ValidationError, match="could not fetch"):
        download_feed_document(f"{origin.base}/slow")

    assert time.perf_counter() - started < 0.9, "the timeout did not apply to the response"


# --- redirects -----------------------------------------------------------------------------


def test_a_redirect_to_a_normal_page_is_followed(origin: Origin, reachable: None) -> None:
    assert download_feed_document(f"{origin.base}/redirect") == FEED
    assert [path for path, _ in origin.requests] == ["/redirect", "/ok"]


def test_a_chain_of_redirects_is_followed(origin: Origin, reachable: None) -> None:
    assert download_feed_document(f"{origin.base}/redirect-twice") == FEED


def test_a_redirect_to_ftp_is_refused_by_the_registrys_own_handler(
    origin: Origin, reachable: None
) -> None:
    """urllib would follow `ftp:`; the handler is what stops it."""
    with pytest.raises(ValidationError, match="refusing to follow a redirect to ftp://"):
        download_feed_document(f"{origin.base}/redirect-ftp")


def test_a_redirect_to_a_local_file_is_refused_and_nothing_is_read(
    origin: Origin, reachable: None
) -> None:
    """urllib itself refuses `file:` redirects (so the registry's handler never sees them); the
    failure surfaces as a fetch error. What matters is that /etc/passwd is not read."""
    with pytest.raises(ValidationError, match=r"could not fetch.*file:///etc/passwd.*not allowed"):
        download_feed_document(f"{origin.base}/redirect-file")


def test_a_redirect_to_a_private_address_is_refused_on_its_own_hop(
    origin: Origin, reachable: None
) -> None:
    """A public host is free to redirect to a private one, so the guard runs on every hop."""
    with pytest.raises(ValidationError, match="not a public address"):
        download_feed_document(f"{origin.base}/redirect-private")


# --- the guard stands in front of the request ----------------------------------------------


def test_loopback_is_refused_before_anything_is_sent(origin: Origin) -> None:
    """No `reachable` fixture: the real guard sees 127.0.0.1 and refuses it. The origin must not
    have received a single request, or the guard would be checking after the damage."""
    with pytest.raises(ValidationError, match="not a public address"):
        download_feed_document(f"{origin.base}/ok")

    assert origin.requests == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://files.example/feed", "gopher://x/"])
def test_a_scheme_other_than_http_is_refused_without_touching_the_network(url: str) -> None:
    with pytest.raises(ValidationError, match="is not an http"):
        download_feed_document(url)


# --- the command, end to end up to the database --------------------------------------------


def test_add_dry_run_prints_the_document_built_from_the_fetched_feed(
    origin: Origin, reachable: None, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main([f"{origin.base}/ok", "--kind", "public", "--country", "BE", "--dry-run"])

    document = json.loads(capsys.readouterr().out)
    assert status == 0
    assert document["metadata"] == {
        "title": "Local Test Library",
        "kind": ["public"],
        "country": "BE",
    }
    assert document["links"][0] == {
        "href": f"{origin.base}/ok",
        "type": "application/opds+json",
        "rel": "catalog",
    }
