"""Scoring a search implementation against the plan's ideal answers, and the page built from it.

`score_case` turns one search into numbers by comparing what came back with what should have:

* **recall**: how many of the expected catalogs came back;
* **precision**: how many of the catalogs that came back were expected (noise lowers it);
* **ndcg**: whether they came back in the right order, first catalogs counting most. Binary
  relevance, or graded when the case says the order matters (`ordered`, or one catalog `first`);
* **score** = ndcg x precision: 1.0 is the ideal answer, in order, with nothing else. A case
  whose ideal is "nothing" scores 1.0 for nothing and 0.0 for anything.

Pure functions only: no database, no network, so the numbers are testable on their own and the
same code scores `today` (what the tests pin), a local run and a deployed instance.
"""

import math
from dataclasses import dataclass

from tests.search_cases import OPENBARE, PARIS, QUALITY_CASES, ROMANDE, RUSSE, Case

NDCG_DEPTH = 10

#: How the plan names catalogs in its tables.
SHORT_NAMES = {
    PARIS: "BnParis",
    OPENBARE: "De Openbare",
    ROMANDE: "BN Romande",
    RUSSE: "Bibliothèque russe et slave",
}


@dataclass(frozen=True)
class CaseScore:
    query: str
    recall: float | None
    precision: float | None
    ndcg: float | None
    score: float


@dataclass(frozen=True)
class Summary:
    cases: int
    perfect: int
    mean_score: float
    mean_recall: float
    mean_precision: float


def _gains(case: Case) -> dict[str, float]:
    """What each expected catalog is worth. Equal, unless the order matters."""
    expected = case.expected or ()
    if not expected:
        return {}
    if case.ordered:
        return {title: float(len(expected) - i) for i, title in enumerate(expected)}
    if case.first is not None:
        return {title: 2.0 if title == case.first else 1.0 for title in expected}
    return dict.fromkeys(expected, 1.0)


def _dcg(gains: list[float]) -> float:
    return sum(g / math.log2(rank + 2) for rank, g in enumerate(gains[:NDCG_DEPTH]))


def score_case(case: Case, returned: list[str]) -> CaseScore | None:
    """Score one search, or `None` when the plan gives no judgement for it."""
    if case.expected is None:
        return None
    if not case.expected:
        nothing = not returned
        return CaseScore(case.query, None, None, None, 1.0 if nothing else 0.0)

    gains = _gains(case)
    found = [title for title in returned if title in gains]
    recall = len(set(found)) / len(gains)
    precision = len(found) / len(returned) if returned else 0.0
    ideal = _dcg(sorted(gains.values(), reverse=True))
    ndcg = _dcg([gains.get(title, 0.0) for title in returned]) / ideal
    return CaseScore(case.query, recall, precision, ndcg, ndcg * precision)


def score_all(
    results: dict[str, list[str]], cases: list[Case] | None = None
) -> list[tuple[Case, list[str], CaseScore | None]]:
    """Every case with what came back for its query (`results` maps query to titles)."""
    return [
        (case, results[case.query], score_case(case, results[case.query]))
        for case in (QUALITY_CASES if cases is None else cases)
    ]


def summarise(scored: list[tuple[Case, list[str], CaseScore | None]]) -> Summary:
    scores = [s for _, _, s in scored if s is not None]
    relevant = [s for s in scores if s.recall is not None and s.precision is not None]
    return Summary(
        cases=len(scores),
        perfect=sum(s.score >= 1.0 - 1e-9 for s in scores),
        mean_score=sum(s.score for s in scores) / len(scores),
        mean_recall=sum(s.recall or 0.0 for s in relevant) / len(relevant),
        mean_precision=sum(s.precision or 0.0 for s in relevant) / len(relevant),
    )


def today_titles() -> dict[str, list[str]]:
    """What the plan says search returns today, as plain titles in rank order."""
    return {case.query: [title for title, _tier in case.today] for case in QUALITY_CASES}


# --- the page for Hadrien --------------------------------------------------------------------


def _name(title: str) -> str:
    return SHORT_NAMES.get(title, title)


def _expected_text(case: Case) -> str:
    if case.expected is None:
        return "no error"
    if not case.expected:
        return "nothing"
    names = ", ".join(_name(t) for t in case.expected)
    if case.first is not None and len(case.expected) > 1:
        return f"{names} ({_name(case.first)} first)"
    if case.first is not None:
        return f"{names} first"
    return names


def _today_text(case: Case) -> str:
    if not case.today:
        return "nothing"
    return ", ".join(
        _name(title) + (" (trigram only)" if tier == 2 else "") for title, tier in case.today
    )


def _cell(text: str) -> str:
    return text.replace("|", "\\|") or "(empty)"


def _query_cell(query: str) -> str:
    if not query:
        return "(empty)"
    if not query.strip():
        return "(spaces only)"
    return f"`{query}`".replace("|", "\\|")


def render_cases_markdown() -> str:
    """The test-case page, generated from `QUALITY_CASES` so it cannot drift from the tests."""
    scored = score_all(today_titles())
    summary = summarise(scored)
    by_section: dict[str, list[tuple[Case, CaseScore | None]]] = {}
    for case, _returned, score in scored:
        by_section.setdefault(case.section, []).append((case, score))

    lines = [
        "# Search test cases",
        "",
        "<!-- Generated by `make search-cases` from tests/search_cases.py. Do not edit by hand: "
        "a test fails when this file is out of date. -->",
        "",
        "These are the searches used to check search on the seed data, and again every time it is "
        "tweaked. The data is `data/recommended.json` (8 catalogs) and `data/libraries.json` "
        "(Bibliothèque numérique de Paris, Lirtuel, De Openbare bibliotheek Vlaanderen en "
        "Brussel, Médiathèque Valais). In the tables, **BnParis** is Bibliothèque numérique de "
        "Paris, **De Openbare** is De Openbare bibliotheek Vlaanderen en Brussel, **BN Romande** "
        "is Bibliothèque numérique Romande.",
        "",
        "**Expected** is the answer we would like. **Today** is what the implementation "
        "returns, pinned exactly by the tests: word matches first, then catalogs found only "
        "through trigrams. **Score** is how close Today is to Expected (see below). A row with a "
        "note is something worth looking at, not a bug in the data.",
        "",
        "Place names are loaded in English plus the country's official languages (French for "
        "France; French, Dutch and German for Belgium; French, German and Italian for "
        "Switzerland).",
        "",
        "## Run them",
        "",
        "```",
        "make search-score                          # score the running server (http://localhost:8000)",
        "make search-score ARGS='--url https://… -v'  # a deployed instance, with every row",
        "make test                                  # the same searches as exact-result tests",
        "```",
        "",
        "* Exact results, over the database: "
        "[`tests/integration/test_search_table.py`](../tests/integration/test_search_table.py)",
        "* Exact results, over HTTP: "
        "[`tests/e2e/test_search_table_http.py`](../tests/e2e/test_search_table_http.py)",
        "* The score: [`tests/search_quality.py`](../tests/search_quality.py), "
        "[`scripts/score_search.py`](../scripts/score_search.py)",
        "* The data these tables come from: [`tests/search_cases.py`](../tests/search_cases.py)",
        "",
        "## Score",
        "",
        f"With today's results the score is **{summary.mean_score:.3f}** over {summary.cases} "
        f"scored searches; {summary.perfect} are exactly the ideal answer. Mean recall "
        f"{summary.mean_recall:.2f}, mean precision {summary.mean_precision:.2f} "
        "(searches whose ideal is nothing are not in those two).",
        "",
        "A search scores **1.0** when it returns the expected catalogs, in the expected order, "
        "and nothing else. It is `ndcg x precision`: *recall* is how many expected catalogs "
        "came back, *precision* is how many of the catalogs returned were expected (noise "
        "lowers it), and *ndcg* rewards the right order (expected catalogs counting more the "
        "earlier they appear). A search whose ideal is *nothing* scores 1.0 for nothing and 0.0 "
        "for anything. The headline number is the mean over all scored searches, so a tweak to "
        "ranking, weights or the typo threshold becomes one number to compare.",
        "",
    ]
    for section, rows in by_section.items():
        lines += [
            f"## {section}",
            "",
            "| Search | What it checks | Expected | Today | Score | Note |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for case, score in rows:
            shown = "—" if score is None else f"{score.score:.2f}"
            lines.append(
                f"| {_query_cell(case.query)} | {_cell(case.checks)} | "
                f"{_cell(_expected_text(case))} | {_cell(_today_text(case))} | {shown} | "
                f"{_cell(case.note) if case.note else ''} |"
            )
        lines.append("")

    lines += [
        "## Pages",
        "",
        "The real page size is 50. With 12 catalogs a page cannot be filled, so the same query is "
        "tested with smaller pages: pages add up to the full list with nothing repeated or "
        "skipped, and two catalogs with the same score keep the same order (import date, then "
        "id). See [`tests/e2e/test_search_pagination.py`](../tests/e2e/test_search_pagination.py) "
        "and [`tests/integration/test_search_table.py`]"
        "(../tests/integration/test_search_table.py).",
        "",
        "## French overseas places and city names",
        "",
        "Synthetic catalogs stand in for libraries that do not exist yet (New Caledonia, Guyane); "
        "the Paris library carries `city: Paris`. Covered by "
        "[`tests/integration/test_search_table.py`](../tests/integration/test_search_table.py) "
        "and [`tests/integration/test_search_lifecycle.py`]"
        "(../tests/integration/test_search_lifecycle.py).",
        "",
        "## What this tells us so far",
        "",
        "Place names work in every language we load, with and without accents, and typos in both "
        "titles and place names are found; plurals come through trigrams. Things to watch: short "
        "common words match because there are no stop words yet, so 'de' pulls in the Belgian "
        "libraries; swapped letters in short words are found only about two times in three; and "
        "a quoted phrase gets fuzzy extras after the exact match.",
        "",
    ]
    return "\n".join(lines)
