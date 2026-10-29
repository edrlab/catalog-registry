"""Deployed key strength, and how the signing-key fetch behaves when its server misbehaves."""

import gc
import http.server
import threading
import time
import warnings
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from registry.core.config import Settings
from registry.core.errors import AdminUnavailableError, AuthenticationError
from registry.main import create_app
from registry.services.admin_identity import build_jwks_key_resolver

pytestmark = [
    pytest.mark.unit,
    # PyJWT leaves the socket of a failed fetch to the garbage collector; not what is tested here.
    pytest.mark.filterwarnings(
        "ignore::ResourceWarning", "ignore::pytest.PytestUnraisableExceptionWarning"
    ),
]

DEPLOYED = {
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "_env_file": None,
    "environment": "production",
    "admin_token_issuer": "https://issuer.example.test",
    "admin_token_audience": "aud",
    "admin_jwks_url": "https://issuer.example.test/certs",
}
# A real-looking RS256 header with a kid, so the resolver must fetch the key set.
TOKEN = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.eyJ4IjoxfQ.AAAA"


def test_a_long_key_starts_when_deployed() -> None:
    assert Settings(service_keys="k" * 32, **DEPLOYED).service_key_values  # type: ignore[arg-type]


def test_a_short_key_is_fine_locally() -> None:
    assert Settings(service_keys="k", **{**DEPLOYED, "environment": "local"}).service_key_values  # type: ignore[arg-type]


def test_the_public_schema_describes_admin_routes_only_locally() -> None:
    def paths(environment: str) -> set[str]:
        settings = Settings(**{**DEPLOYED, "environment": environment})  # type: ignore[arg-type]
        return set(create_app(settings).openapi()["paths"])

    assert "/admin/me" in paths("local")
    assert not any(path.startswith("/admin") for path in paths("production"))
    assert "/search" in paths("production")


@pytest.mark.parametrize("weak", ["k", "short-key", "CHANGE-ME", "k" * 31])
def test_a_weak_service_key_refuses_to_start_when_deployed(weak: str) -> None:
    with pytest.raises(ValidationError):
        Settings(service_keys=weak, **DEPLOYED)  # type: ignore[arg-type]


def close_failed_response(error: BaseException | None) -> None:
    """urllib keeps the socket of an HTTP error response open until it is closed; close it here."""
    while error is not None:
        if hasattr(error, "close"):
            error.close()
        error = error.__cause__ or error.__context__


@pytest.fixture
def key_server() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"mode": "garbage", "hits": 0, "redirect_followed": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/elsewhere":
                state["redirect_followed"] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"keys": []}')
                return
            state["hits"] += 1
            if state["mode"] == "slow":
                time.sleep(6)  # past the 5 s timeout; the client has gone by now
                return
            if state["mode"] == "redirect":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
                return
            code, body = {"garbage": (200, b"<html>not json</html>"), "error": (500, b"oops")}.get(
                state["mode"], (200, b"{}")
            )
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/certs"
    yield state
    server.shutdown()
    server.server_close()  # waits for a handler still sleeping, so it ends inside this test
    # Collect the socket PyJWT left open here, not in some later test's setup.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()


async def test_a_key_set_that_is_not_json_is_an_outage_not_a_bad_token(
    key_server: dict[str, Any],
) -> None:
    resolve = build_jwks_key_resolver(key_server["url"])

    with pytest.raises(AdminUnavailableError):
        await resolve(TOKEN)


async def test_a_garbage_key_set_is_not_refetched_by_every_request(
    key_server: dict[str, Any],
) -> None:
    resolve = build_jwks_key_resolver(key_server["url"])

    for _ in range(5):
        with pytest.raises((AdminUnavailableError, AuthenticationError)):
            await resolve(TOKEN)

    assert key_server["hits"] <= 2


async def test_a_key_server_error_is_503_and_remembered(key_server: dict[str, Any]) -> None:
    key_server["mode"] = "error"
    resolve = build_jwks_key_resolver(key_server["url"])

    for _ in range(3):
        with pytest.raises(AdminUnavailableError) as caught:
            await resolve(TOKEN)
        close_failed_response(caught.value)

    assert key_server["hits"] == 1


async def test_a_slow_key_server_is_cut_at_the_timeout_and_then_answered_at_once(
    key_server: dict[str, Any],
) -> None:
    key_server["mode"] = "slow"
    resolve = build_jwks_key_resolver(key_server["url"])

    started = time.monotonic()
    with pytest.raises(AdminUnavailableError):
        await resolve(TOKEN)
    first = time.monotonic() - started
    started = time.monotonic()
    with pytest.raises(AdminUnavailableError):
        await resolve(TOKEN)

    assert first < 6.5
    assert time.monotonic() - started < 0.1


async def test_the_key_set_fetch_does_not_follow_redirects(key_server: dict[str, Any]) -> None:
    key_server["mode"] = "redirect"
    resolve = build_jwks_key_resolver(key_server["url"])

    with pytest.raises(AdminUnavailableError) as caught:
        await resolve(TOKEN)
    close_failed_response(caught.value)

    assert key_server["redirect_followed"] == 0
