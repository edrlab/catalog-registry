"""Validate the demo fixtures against the repository's own schemas.

If the fixtures and the schema disagree, one of them is wrong and that should be a build
failure, not a discovery.
"""

import json
import sys
from pathlib import Path
from typing import Any

from registry.core.schema_validation import build_schema_validator, load_json_document

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Seed inputs, validated against the relaxed schema and never the published one.
#: `(path, schema, required)`. An optional file that is absent is reported as skipped rather than
#: failing. `data/libraries.json` arrives with edrlab/catalog-registry#13, and this list is what
#: picks it up when it does. `required` exists so a **typo** in a path cannot skip quietly for
#: ever: `demo/` always contributes fixtures, so the `checked == 0` backstop below would never
#: notice one.
SEED_INPUTS = [
    (Path("data") / "recommended.json", "generated/seed-input.schema.json", True),
    (Path("data") / "libraries.json", "generated/seed-input.schema.json", False),
]

#: Fixture glob → the schema that governs it.
FIXTURE_SCHEMAS = {
    "catalogs/*.json": "catalog.schema.json",
    "index.json": "feed.schema.json",
    "search.json": "feed.schema.json",
}


def report(errors: list[Any], label: str) -> bool:
    """Print one file's verdict. True when it failed."""
    if not errors:
        print(f"ok   {label}")
        return False
    print(f"FAIL {label}")
    for error in errors:
        print(f"       {list(error.absolute_path)}: {error.message}")
    return True


def check_seed_inputs() -> tuple[int, int]:
    """Validate every entry in `SEED_INPUTS`. Returns (checked, failures)."""
    checked = failures = 0
    for seed_path, seed_schema, required in SEED_INPUTS:
        if not (REPO_ROOT / seed_path).exists():
            if required:
                failures += 1
                print(f"FAIL {seed_path} is missing, and is not optional")
            else:
                print(f"skip {seed_path}, not in this checkout")
            continue
        checked += 1
        errors = sorted(
            build_schema_validator(seed_schema).iter_errors(
                load_json_document(REPO_ROOT / seed_path)
            ),
            key=str,
        )
        failures += report(errors, f"{seed_path} against {seed_schema}")
    return checked, failures


def main(argv: list[str]) -> int:
    fixture_root = REPO_ROOT / (argv[0] if argv else "demo")
    checked, failures = check_seed_inputs()

    for pattern, schema_name in FIXTURE_SCHEMAS.items():
        validator = build_schema_validator(schema_name)
        for path in sorted(fixture_root.glob(pattern)):
            checked += 1
            relative = path.relative_to(REPO_ROOT)
            errors = sorted(
                validator.iter_errors(json.loads(path.read_text(encoding="utf-8"))), key=str
            )
            failures += report(errors, f"{relative} against {schema_name}")

    if checked == 0:
        print(f"FAIL no fixtures found under {fixture_root}")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
