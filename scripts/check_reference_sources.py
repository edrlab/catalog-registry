"""Has CLDR or pycountry published a release newer than the one the reference data is built from?

    uv run python scripts/check_reference_sources.py [--github-output FILE]

The registry's country languages, country names, subdivisions and subdivision names come from two
pinned sources (ADR-057): CLDR (`CLDR_VERSION` in `generate_reference_data.py`) and pycountry (the
`pycountry==` pin in the Makefile). Nothing tells us when they move, so this asks: GitHub for the
latest cldr-json release, PyPI for the latest pycountry. Run weekly by
`.github/workflows/reference-data-watch.yaml`, which opens an issue when something is newer.

Processing a release is never automatic: regenerate with the new pin, review the difference, and
ship it as a new data migration (`generate_reference_data.py --update DIR`).
"""

import argparse
import json
import re
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLDR_LATEST = "https://api.github.com/repos/unicode-org/cldr-json/releases/latest"
PYCOUNTRY_LATEST = "https://pypi.org/pypi/pycountry/json"
USER_AGENT = "thorium-catalog-registry reference-data-watch"

Fetch = Callable[[str], dict[str, object]]


@dataclass(frozen=True, slots=True)
class SourceStatus:
    name: str
    pinned: str
    latest: str

    @property
    def outdated(self) -> bool:
        return parse_version(self.latest) > parse_version(self.pinned)


def parse_version(version: str) -> tuple[int, ...]:
    """`48.2.0`, `v49.0.0` and `26.2.16` as comparable tuples; pre-release suffixes are ignored."""
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-", maxsplit=1)[0])[:3])


def read_pins(root: Path = REPO_ROOT) -> dict[str, str]:
    generator = (root / "scripts" / "generate_reference_data.py").read_text(encoding="utf-8")
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    cldr = re.search(r'^CLDR_VERSION = "([^"]+)"', generator, re.MULTILINE)
    pycountry = re.search(r"pycountry==([0-9.]+)", makefile)
    if not cldr or not pycountry:
        raise SystemExit("could not read the pinned CLDR or pycountry version")
    return {"CLDR": cldr.group(1), "pycountry": pycountry.group(1)}


def fetch_json(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        document: dict[str, object] = json.load(response)
    return document


def build_statuses(pins: dict[str, str], fetch: Fetch = fetch_json) -> list[SourceStatus]:
    cldr = str(fetch(CLDR_LATEST)["tag_name"])
    pycountry_info = fetch(PYCOUNTRY_LATEST)["info"]
    assert isinstance(pycountry_info, dict)
    return [
        SourceStatus("CLDR", pins["CLDR"], cldr.removeprefix("v")),
        SourceStatus("pycountry", pins["pycountry"], str(pycountry_info["version"])),
    ]


def render_report(statuses: list[SourceStatus]) -> str:
    lines = [
        f"- {status.name}: pinned {status.pinned}, latest {status.latest}"
        + (" — newer release available" if status.outdated else " — up to date")
        for status in statuses
    ]
    if any(status.outdated for status in statuses):
        lines += [
            "",
            "To process it: bump the pin (`CLDR_VERSION` in `scripts/generate_reference_data.py`, "
            "or `pycountry==` in the Makefile), run "
            "`uv run --with pycountry==<pin> python scripts/generate_reference_data.py "
            "--update migrations/data/<new revision>`, review the CSVs, and add a data migration "
            "that inserts them (as `c155bd2aa2f3` does).",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--github-output", type=Path, help="append outdated=… and a report here")
    args = parser.parse_args(argv)
    statuses = build_statuses(read_pins())
    report = render_report(statuses)
    print(report)
    if args.github_output:
        outdated = any(status.outdated for status in statuses)
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"outdated={'true' if outdated else 'false'}\n")
            handle.write(f"report<<EOF\n{report}\nEOF\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
