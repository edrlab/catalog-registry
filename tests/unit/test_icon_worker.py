"""`persist_catalog_icon`: what the registry sends the image Worker, and how it reads the answer."""

from functools import partial

import httpx
import pytest
from pydantic import SecretStr

from registry.core.config import Settings
from registry.core.errors import AdminUnavailableError, FieldValidationError
from registry.domain.fetching import FetchError
from registry.network.icon_worker import (
    ICON_IS_SVG,
    ICON_NOT_IMAGE,
    ICON_TOO_LARGE,
    fetch_catalog_icon,
    persist_catalog_icon,
)
from registry.network.safe_client import SafeHttpClient
from registry.services.admin_service import describe_icon_failure

CATALOG_ID = "0b6f7c1e-3d2a-4f5b-9c8d-7e6f5a4b3c2d"
SETTINGS = Settings(
    database_url="postgresql+asyncpg://unused@localhost/unused",
    environment="test",
    icon_worker_url="https://worker.example.org/",
    icon_worker_secret=SecretStr("worker-secret"),
    icon_base_url="https://icons.example.org/",
)


def answer_with(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> list[httpx.Request]:
    """Route the module's client to *response*; returns the requests it received."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    return seen


async def test_the_image_goes_to_the_worker_and_the_url_carries_the_stored_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = answer_with(
        monkeypatch,
        httpx.Response(200, json={"key": f"{CATALOG_ID}-icon.png", "etag": "abc123"}),
    )

    url = await persist_catalog_icon(SETTINGS, CATALOG_ID, b"bytes", "image/webp")

    assert url == f"https://icons.example.org/{CATALOG_ID}-icon.png?v=abc123"
    (request,) = seen
    assert (request.method, str(request.url)) == ("PUT", f"https://worker.example.org/{CATALOG_ID}")
    assert request.headers["authorization"] == "Bearer worker-secret"
    assert (request.headers["content-type"], request.content) == ("image/webp", b"bytes")


@pytest.mark.parametrize("status", [400, 413, 415, 422])
async def test_an_image_the_worker_cannot_read_is_a_422(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    answer_with(monkeypatch, httpx.Response(status))

    with pytest.raises(FieldValidationError):
        await persist_catalog_icon(SETTINGS, CATALOG_ID, b"bytes", "image/png")


@pytest.mark.parametrize("status", [401, 500, 502])
async def test_any_other_worker_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    answer_with(monkeypatch, httpx.Response(status))

    with pytest.raises(AdminUnavailableError):
        await persist_catalog_icon(SETTINGS, CATALOG_ID, b"bytes", "image/png")


async def test_unconfigured_is_unavailable_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = answer_with(monkeypatch, httpx.Response(200))
    bare = SETTINGS.model_copy(
        update={"icon_worker_url": None, "icon_worker_secret": None, "icon_base_url": None}
    )

    with pytest.raises(AdminUnavailableError):
        await persist_catalog_icon(bare, CATALOG_ID, b"bytes", "image/png")
    assert seen == []


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"key": f"{CATALOG_ID}-icon.png"}),
        httpx.Response(200, json={"key": "someone-else-icon.png", "etag": "abc123"}),
        httpx.Response(200, json={"key": f"{CATALOG_ID}-icon.png", "etag": "a b<script>"}),
    ],
    ids=["not-json", "not-an-object", "no-etag", "another-key", "etag-not-plain"],
)
async def test_an_unexpected_answer_is_unavailable_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, answer: httpx.Response
) -> None:
    answer_with(monkeypatch, answer)

    with pytest.raises(AdminUnavailableError):
        await persist_catalog_icon(SETTINGS, CATALOG_ID, b"bytes", "image/png")


def problem(status: int, detail: object) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "type": "about:blank",
            "title": "Unprocessable Content",
            "status": status,
            "detail": detail,
        },
        headers={"content-type": "application/problem+json"},
    )


NOT_READ = "Not an image that can be read"
UNSUPPORTED = "We don't support this icon's format{} yet, so it can't be saved on our servers"


@pytest.mark.parametrize(
    ("answer", "image", "shown"),
    [
        (
            problem(422, "SVG cannot be converted; send PNG"),
            b"<svg/>",
            UNSUPPORTED.format(" (SVG)"),
        ),
        (problem(422, NOT_READ), b"\x00\x00\x01\x00\x01\x00", UNSUPPORTED.format(" (ICO)")),
        (problem(422, NOT_READ), b"BM6\x00\x00\x00", UNSUPPORTED.format(" (BMP)")),
        (problem(422, NOT_READ), b"<!doctype html>", UNSUPPORTED.format("")),
        (problem(413, "Send an image of at most 5 MB"), b"bytes", ICON_TOO_LARGE),
        (httpx.Response(422, text="plain text, not a problem"), b"bytes", UNSUPPORTED.format("")),
        (problem(422, 42), b"bytes", UNSUPPORTED.format("")),
    ],
    ids=["svg", "ico", "bmp", "unknown", "too-large", "not-json", "detail-not-text"],
)
async def test_the_editor_reads_plain_words_naming_the_format_when_known(
    monkeypatch: pytest.MonkeyPatch, answer: httpx.Response, image: bytes, shown: str
) -> None:
    answer_with(monkeypatch, answer)

    with pytest.raises(FieldValidationError) as refused:
        await persist_catalog_icon(SETTINGS, CATALOG_ID, image, "image/png")
    assert refused.value.errors == [{"field": "body", "message": shown}]


@pytest.mark.parametrize(
    ("error", "sentence"),
    [
        (
            FetchError("fetch-failed", "The site answered 403.", url="https://x.example"),
            "We couldn't fetch the icon from the library's website just now, so its own icon is "
            "used for now. Try again later.",
        ),
        (
            FetchError("fetch-too-large", "Over 5 MB.", url="https://x.example"),
            "This icon is too large to copy, so the library's own icon is used.",
        ),
        (
            FieldValidationError([{"field": "body", "message": ICON_IS_SVG}]),
            "We don't support this icon's format (SVG) yet, so it can't be saved on our servers. "
            "The library's own icon is used.",
        ),
        (
            FieldValidationError([{"field": "body", "message": ICON_NOT_IMAGE}]),
            "We don't support this icon's format yet, so it can't be saved on our servers. "
            "The library's own icon is used.",
        ),
        (
            AdminUnavailableError("The image service answered 502."),
            "Icons can't be copied right now. Try again later.",
        ),
    ],
    ids=["site-refused", "too-large", "svg", "not-an-image", "converter-down"],
)
def test_each_icon_failure_is_one_plain_sentence(error: Exception, sentence: str) -> None:
    assert describe_icon_failure(error) == sentence


LIBRARY_ICON = "https://library.example.org/apple-touch-icon.png"


def site_answers(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, headers: dict[str, str] | None = None
) -> None:
    """Every request (a 503 is retried) gets a fresh answer, streamed as a server sends it;
    retries do not wait."""

    async def no_wait(_: float) -> None:
        return None

    transport = httpx.MockTransport(
        lambda _: httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))
    )
    client = partial(SafeHttpClient, transport=transport, sleep=no_wait)
    monkeypatch.setattr("registry.network.icon_worker.SafeHttpClient", client)


async def test_a_downloaded_icon_comes_with_its_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    site_answers(monkeypatch, 200, b"png", {"content-type": "image/png"})

    assert await fetch_catalog_icon(LIBRARY_ICON) == (b"png", "image/png")


async def test_a_site_that_does_not_answer_200_is_a_fetch_error_naming_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site_answers(monkeypatch, 503, b"<html>maintenance</html>")

    with pytest.raises(FetchError) as raised:
        await fetch_catalog_icon(LIBRARY_ICON)

    assert raised.value.detail == "The site answered 503."
