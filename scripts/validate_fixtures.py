"""Validate the demo fixtures against the repository's own schemas.

If the fixtures and the schema disagree, one of them is wrong and that should be a build
failure, not a discovery.
"""

import json
import sys
from pathlib import Path

from registry.core.schema_validation import build_schema_validator, load_json_document

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The seed input is validated against the relaxed schema, never the published one.
SEED_INPUT = (Path("data") / "recommended.json", "generated/seed-input.schema.json")

#: Fixture glob → the schema that governs it.
FIXTURE_SCHEMAS = {
    "catalogs/*.json": "catalog.schema.json",
    "index.json": "feed.schema.json",
    "search.json": "feed.schema.json",
}


def main(argv: list[str]) -> int:
    fixture_root = REPO_ROOT / (argv[0] if argv else "demo")
    failures = 0
    checked = 0

    seed_path, seed_schema = SEED_INPUT
    if (REPO_ROOT / seed_path).exists():
        checked += 1
        errors = sorted(
            build_schema_validator(seed_schema).iter_errors(
                load_json_document(REPO_ROOT / seed_path)
            ),
            key=str,
        )
        if errors:
            failures += 1
            print(f"FAIL {seed_path} against {seed_schema}")
            for error in errors:
                print(f"       {list(error.absolute_path)}: {error.message}")
        else:
            print(f"ok   {seed_path} against {seed_schema}")

    for pattern, schema_name in FIXTURE_SCHEMAS.items():
        validator = build_schema_validator(schema_name)
        for path in sorted(fixture_root.glob(pattern)):
            checked += 1
            relative = path.relative_to(REPO_ROOT)
            errors = sorted(
                validator.iter_errors(json.loads(path.read_text(encoding="utf-8"))), key=str
            )
            if errors:
                failures += 1
                print(f"FAIL {relative} against {schema_name}")
                for error in errors:
                    print(f"       {list(error.absolute_path)}: {error.message}")
            else:
                print(f"ok   {relative} against {schema_name}")

    if checked == 0:
        print(f"FAIL no fixtures found under {fixture_root}")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
