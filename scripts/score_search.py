"""Score a running registry's search against the plan's ideal answers.

    make search-score                              # http://localhost:8000
    make search-score ARGS="--url https://registry.example -v"
    make search-score ARGS="--save before.json"    # then tweak, run again with:
    make search-score ARGS="--baseline before.json"

Runs every search of `tests/search_cases.py` against `GET /search` and reports, per search, whether
the expected catalogs came back and where, and overall one number (see `tests/search_quality.py`).
Read-only: it only issues searches. `--min-score` makes it exit 1 below a floor and `--baseline`
exits 1 when the score dropped, which is what lets a tweak be judged better or worse in CI.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Run as a file, `tests` is not importable until the repository root is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.search_quality import (
    CaseScore,
    score_all,
    summarise,
)

TIMEOUT_SECONDS = 15
EPSILON = 1e-9


def fetch_search_titles(base_url: str, query: str) -> list[str]:
    """Titles in rank order for page 1 of *query*. An empty query is sent as such."""
    url = f"{base_url.rstrip('/')}/search?{urllib.parse.urlencode({'query': query})}"
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
        body: dict[str, Any] = json.load(response)
    return [catalog["metadata"]["title"] for catalog in body["catalogs"]]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://localhost:8000", help="the registry to score")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every search")
    parser.add_argument("--save", type=Path, help="write the per-search scores to this file")
    parser.add_argument("--baseline", type=Path, help="compare with a file written by --save")
    parser.add_argument("--min-score", type=float, help="exit 1 when the mean score is below this")
    args = parser.parse_args(argv)

    from tests.search_cases import QUALITY_CASES  # noqa: PLC0415

    try:
        results = {case.query: fetch_search_titles(args.url, case.query) for case in QUALITY_CASES}
    except (urllib.error.URLError, OSError) as error:
        print(f"{args.url} is not answering ({error}). Is the server running (`make up`)?")
        return 2

    scored = score_all(results)
    summary = summarise(scored)
    previous: dict[str, float] = {}
    if args.baseline:
        previous = json.loads(args.baseline.read_text(encoding="utf-8"))["scores"]

    def show(query: str, score: CaseScore) -> str:
        before = previous.get(query)
        delta = "" if before is None else f"  ({score.score - before:+.2f})"
        return f"{score.score:.2f}{delta}"

    print(f"{args.url}  {summary.cases} scored searches\n")
    for case, returned, score in scored:
        if score is None:
            continue
        imperfect = score.score < 1.0 - EPSILON
        moved = case.query in previous and abs(score.score - previous[case.query]) > EPSILON
        if not (args.verbose or imperfect or moved):
            continue
        want = ", ".join(case.expected or ()) or "nothing"
        got = ", ".join(returned) or "nothing"
        print(
            f"{'!' if imperfect else ' '} {show(case.query, score):<14} "
            f"{case.query!r:<28} wanted: {want}"
        )
        if imperfect or args.verbose:
            print(f"{'':<46}got:    {got}")

    print(
        f"\nscore {summary.mean_score:.3f}   perfect {summary.perfect}/{summary.cases}   "
        f"recall {summary.mean_recall:.2f}   precision {summary.mean_precision:.2f}"
    )

    if args.save:
        scores = {s.query: s.score for _, _, s in scored if s is not None}
        args.save.write_text(
            json.dumps({"mean": summary.mean_score, "scores": scores}, indent=2, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        print(f"saved {args.save}")

    status = 0
    if args.baseline:
        before_mean = json.loads(args.baseline.read_text(encoding="utf-8"))["mean"]
        direction = (
            "better"
            if summary.mean_score > before_mean + EPSILON
            else ("worse" if summary.mean_score < before_mean - EPSILON else "unchanged")
        )
        print(
            f"vs baseline {before_mean:.3f}: {direction} ({summary.mean_score - before_mean:+.3f})"
        )
        status = 1 if direction == "worse" else 0
    if args.min_score is not None and summary.mean_score < args.min_score:
        print(f"below the floor of {args.min_score}")
        status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
