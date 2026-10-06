"""Write `docs/search-test-cases.md` from `tests/search_cases.py`. `make search-cases`."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.search_quality import render_cases_markdown  # noqa: E402

TARGET = ROOT / "docs" / "search-test-cases.md"

if __name__ == "__main__":
    TARGET.write_text(render_cases_markdown(), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
