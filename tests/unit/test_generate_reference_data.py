"""The reference data generator's download rule (CodeRabbit finding, 2 October).

CLDR genuinely has no file for some languages, so a 404 is skipped. Anything else (network,
5xx, a broken body) must stop the run: swallowing it would write incomplete CSVs without a word.
Offline: `urlopen` is replaced, nothing is fetched.
"""

import io
import urllib.error
import urllib.request
from pathlib import Path

import generate_reference_data as generator
import pytest

pytestmark = pytest.mark.unit

URL = "https://example.invalid/cldr/xx/territories.json"


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "x", None, io.BytesIO(b""))  # type: ignore[arg-type]


class Response(io.BytesIO):
    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_a_download_is_cached_and_the_second_read_needs_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> Response:
        calls.append(url)
        return Response(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fetch)

    assert generator.download_cached(URL, tmp_path) == '{"ok": true}'
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: pytest.fail("went to the network")
    )

    assert generator.download_cached(URL, tmp_path) == '{"ok": true}'
    assert len(calls) == 1


def test_a_404_is_reported_as_a_missing_file_and_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise http_error(404)

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(FileNotFoundError):
        generator.download_cached(URL, tmp_path)

    # Remembered, so a rerun of the generator is offline even for the languages CLDR lacks.
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: pytest.fail("went to the network")
    )
    with pytest.raises(FileNotFoundError):
        generator.download_cached(URL, tmp_path)


@pytest.mark.parametrize("code", [500, 502, 503, 403, 429])
def test_any_other_http_error_stops_the_run(
    code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_: object, **__: object) -> None:
        raise http_error(code)

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(urllib.error.HTTPError) as raised:
        generator.download_cached(URL, tmp_path)

    assert raised.value.code == code
    assert not list(tmp_path.iterdir()), "a failed download must leave nothing behind to trust"


def test_a_network_failure_stops_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*_: object, **__: object) -> None:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", down)

    with pytest.raises(urllib.error.URLError):
        generator.download_cached(URL, tmp_path)


def test_a_missing_locale_is_skipped_but_an_outage_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour the rule exists for, one level up: country names skip a language CLDR has no
    file for, and stop on a server error."""
    monkeypatch.setattr(
        generator,
        "download_territory_names",
        lambda lang, cache: (_ for _ in ()).throw(FileNotFoundError(lang)),
    )
    names = generator.generate_country_names({"AD"}, {"AD": ["xx"]}, tmp_path)
    assert names == []

    monkeypatch.setattr(
        generator,
        "download_territory_names",
        lambda lang, cache: (_ for _ in ()).throw(http_error(503)),
    )
    with pytest.raises(urllib.error.HTTPError):
        generator.generate_country_names({"AD"}, {"AD": ["xx"]}, tmp_path)


def test_every_overseas_subdivision_has_a_territory_letter_code() -> None:
    """ADR-057: the generator's overseas rule maps FR-NC to NC, FR-973 to GF and so on."""
    assert generator.OVERSEAS_TERRITORY["FR-NC"] == "NC"
    assert generator.OVERSEAS_TERRITORY["FR-973"] == "GF"
    assert all(code.startswith("FR-") for code in generator.OVERSEAS_TERRITORY)
