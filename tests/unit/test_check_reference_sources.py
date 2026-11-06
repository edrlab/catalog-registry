"""The reference-data watcher: reads the pins, compares versions, says what to do. Offline."""

from pathlib import Path

import check_reference_sources as watcher
import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("version", "parsed"),
    [
        ("48.2.0", (48, 2, 0)),
        ("v49.0.0", (49, 0, 0)),
        ("26.2.16", (26, 2, 16)),
        ("49-beta4", (49,)),
    ],
)
def test_versions_parse(version: str, parsed: tuple[int, ...]) -> None:
    assert watcher.parse_version(version) == parsed


def test_the_pins_are_read_from_the_generator_and_the_makefile() -> None:
    pins = watcher.read_pins()

    assert watcher.parse_version(pins["CLDR"]) >= (48, 2, 0)
    assert watcher.parse_version(pins["pycountry"]) >= (26, 2, 16)


def test_missing_pins_stop_the_check(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "generate_reference_data.py").write_text("")
    (tmp_path / "Makefile").write_text("")

    with pytest.raises(SystemExit):
        watcher.read_pins(tmp_path)


def fake_fetch(cldr: str, pycountry: str) -> watcher.Fetch:
    def fetch(url: str) -> dict[str, object]:
        if url == watcher.CLDR_LATEST:
            return {"tag_name": cldr}
        return {"info": {"version": pycountry}}

    return fetch


def test_a_newer_cldr_is_reported_with_what_to_do() -> None:
    statuses = watcher.build_statuses(
        {"CLDR": "48.2.0", "pycountry": "26.2.16"}, fake_fetch("49.0.0", "26.2.16")
    )

    assert [(s.name, s.outdated) for s in statuses] == [("CLDR", True), ("pycountry", False)]
    report = watcher.render_report(statuses)
    assert "CLDR: pinned 48.2.0, latest 49.0.0" in report
    assert "--update" in report


def test_nothing_newer_says_up_to_date() -> None:
    statuses = watcher.build_statuses(
        {"CLDR": "49.0.0", "pycountry": "26.2.16"}, fake_fetch("49.0.0", "26.2.16")
    )

    assert not any(status.outdated for status in statuses)
    assert "To process it" not in watcher.render_report(statuses)


def test_github_output_carries_the_flag_and_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        watcher, "build_statuses", lambda _pins: [watcher.SourceStatus("CLDR", "48.2.0", "49.0.0")]
    )
    output = tmp_path / "out"

    assert watcher.main(["--github-output", str(output)]) == 0

    text = output.read_text()
    assert "outdated=true" in text
    assert "report<<EOF" in text
