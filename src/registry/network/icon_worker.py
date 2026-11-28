"""The image Worker: turns an uploaded icon into a PNG and stores it in object storage.

Its contract: `PUT {worker}/{catalog_id}` with the image bytes as the body and
`Authorization: Bearer {secret}`. It answers 200 `{"key": "<catalog_id>-icon.png", "etag": "..."}`,
or 4xx when the bytes are not an image it can read. The address is ours, from the settings, not
one someone typed, so this does not go through `SafeHttpClient`.
"""

from typing import Final

import httpx

from registry.core.config import Settings
from registry.core.errors import AdminUnavailableError, FieldValidationError
from registry.domain.fetching import FetchError
from registry.network.safe_client import SafeHttpClient

#: The catalog row stays locked while the icon is downloaded and the Worker answers, so the two
#: together stay well under the write session's 60 s idle-in-transaction limit.
ICON_WORKER_TIMEOUT_SECONDS: Final = 20.0
ICON_FETCH_SECONDS: Final = 10.0
#: An icon larger than this is refused before anything is sent on. Real icons are a few hundred kB.
MAX_ICON_BYTES: Final = 5 * 1024 * 1024

#: The Worker's answers for bytes it cannot turn into an icon.
_REFUSED_IMAGE: Final = frozenset({400, 413, 415, 422})
#: What an editor reads when the image service refuses the file: plain words, no codes.
ICON_TOO_LARGE: Final = "This icon is too large to copy"
ICON_NOT_IMAGE: Final = (
    "We don't support this icon's format yet, so it can't be saved on our servers"
)
ICON_IS_SVG: Final = ICON_NOT_IMAGE.replace("format", "format (SVG)")
#: Formats told from a refused file's first bytes, to name them to the editor.
_SIGNATURES: Final = ((b"\x00\x00\x01\x00", "ICO"), (b"\x00\x00\x02\x00", "CUR"), (b"BM", "BMP"))


async def persist_catalog_icon(
    settings: Settings, catalog_id: str, image: bytes, media_type: str
) -> str:
    """Store *image* as the catalog's icon and return its public URL. The URL carries the stored
    object's etag as `?v=`, so a reader that caches icons by URL picks up a replaced one."""
    worker, secret, base = (
        settings.icon_worker_url,
        settings.icon_worker_secret,
        settings.icon_base_url,
    )
    if not (worker and secret and base):
        raise AdminUnavailableError("Icon uploads are not configured.")
    try:
        async with httpx.AsyncClient(timeout=ICON_WORKER_TIMEOUT_SECONDS) as client:
            response = await client.put(
                f"{worker.rstrip('/')}/{catalog_id}",
                content=image,
                headers={
                    "Authorization": f"Bearer {secret.get_secret_value()}",
                    "Content-Type": media_type,
                },
            )
    except httpx.HTTPError as error:
        raise AdminUnavailableError("The image service could not be reached.") from error
    if response.status_code in _REFUSED_IMAGE:
        message = _read_refusal(response, image)
        raise FieldValidationError([{"field": "body", "message": message}])
    if response.status_code != httpx.codes.OK:
        raise AdminUnavailableError(f"The image service answered {response.status_code}.")
    # The answer becomes a public link, so it is checked, not trusted: our key, a plain-hex etag.
    try:
        stored = response.json()
        key, etag = stored["key"], stored["etag"]
    except (ValueError, KeyError, TypeError) as error:
        raise AdminUnavailableError("The image service gave an unreadable answer.") from error
    if key != f"{catalog_id}-icon.png" or not isinstance(etag, str) or not etag.isalnum():
        raise AdminUnavailableError("The image service gave an unexpected answer.")
    return f"{base.rstrip('/')}/{key}?v={etag}"


async def fetch_catalog_icon(href: str) -> tuple[bytes, str]:
    """Download a library's icon, with the protections every outbound fetch has. Raises
    `FetchError` when it does not arrive whole with a 200."""
    async with SafeHttpClient() as client:
        result = await client.fetch(
            href, accept="image/*", max_bytes=MAX_ICON_BYTES, deadline=ICON_FETCH_SECONDS
        )
    if result.status != httpx.codes.OK or not result.body:
        raise FetchError("fetch-failed", f"The site answered {result.status}.", url=href)
    return result.body, result.media_type or "application/octet-stream"


def _read_refusal(response: httpx.Response, image: bytes) -> str:
    """The editor's words for the Worker's refusal, naming the format when it is known. Its RFC 9457
    `detail` is for us, not them; the SVG refusal is told apart by it: it starts with "SVG"
    (`workers/icon-processing`)."""
    if response.status_code == httpx.codes.REQUEST_ENTITY_TOO_LARGE:
        return ICON_TOO_LARGE
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail.startswith("SVG"):
        return ICON_IS_SVG
    name = next((name for magic, name in _SIGNATURES if image.startswith(magic)), None)
    return ICON_NOT_IMAGE.replace("format", f"format ({name})") if name else ICON_NOT_IMAGE
