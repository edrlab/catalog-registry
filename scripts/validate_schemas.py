"""Assert every file in schema/ is valid JSON and a valid draft-07 schema.

The trailing comma in `catalog.schema.json` sat unnoticed until it was read by eye. This
makes that impossible to repeat, which matters because Hadrien edits the schemas directly.
"""

import json
import sys

from registry.core.schema_validation import (
    SCHEMA_DIR,
    check_schema_is_valid,
    load_schema_document,
)


def main() -> int:
    failures = 0
    for path in sorted(SCHEMA_DIR.rglob("*.json")):
        relative = path.relative_to(SCHEMA_DIR.parent)
        try:
            check_schema_is_valid(load_schema_document(path))
        except json.JSONDecodeError as exc:
            print(f"FAIL {relative}: invalid JSON — {exc}")
            failures += 1
        except Exception as exc:
            print(f"FAIL {relative}: invalid draft-07 schema — {exc}")
            failures += 1
        else:
            print(f"ok   {relative}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
