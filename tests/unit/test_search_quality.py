"""The score, the cases it reads, the page built from them, and the command that runs it.

Pure: no database, no server. The numbers are worked out by hand in each test, so a change to
the formula has to be a decision, not a side effect.
"""

import json
import math
from pathlib import Path

import pytest
import score_search

from tests.conftest import LIBRARIES_FILE, REPO_ROOT, SEED_FILE
from tests.search_cases import QUALITY_CASES, Case
from tests.search_helpers import titles_in
from tests.search_quality import (
    render_cases_markdown,
    score_all,
    score_case,
    summarise,
    today_titles,
)

pytestmark = pytest.mark.unit

A, B, C = "Alpha", "Bravo", "Charlie"


def case(
    expected: tuple[str, ...] | None,
    *,
    ordered: bool = False,
    first: str | None = None,
) -> Case:
    return Case("s", "q", "checks", expected, [], ordered=ordered, first=first)


# --- one search ------------------------------------------------------------------------------


def test_the_ideal_answer_scores_one() -> None:
    result = score_case(case((A, B)), [A, B])

    assert result is not None
    assert (result.recall, result.precision, result.ndcg, result.score) == (1.0, 1.0, 1.0, 1.0)


def test_noise_lowers_precision_and_so_the_score() -> None:
    result = score_case(case((A,)), [A, B, C])

    assert result is not None
    assert result.recall == 1.0
    assert result.precision == pytest.approx(1 / 3)
    assert result.ndcg == 1.0
    assert result.score == pytest.approx(1 / 3)


def test_a_missing_catalog_lowers_recall() -> None:
    result = score_case(case((A, B)), [A])

    assert result is not None
    assert result.recall == 0.5
    assert result.precision == 1.0
    # DCG of [1] is 1; the ideal DCG of [1, 1] is 1 + 1/log2(3).
    assert result.ndcg == pytest.approx(1 / (1 + 1 / math.log2(3)))


def test_finding_nothing_when_something_was_expected_scores_zero() -> None:
    result = score_case(case((A,)), [])

    assert result is not None
    assert (result.recall, result.precision, result.ndcg, result.score) == (0.0, 0.0, 0.0, 0.0)


def test_only_the_wrong_catalog_scores_zero() -> None:
    result = score_case(case((A,)), [B])

    assert result is not None
    assert (result.recall, result.precision, result.score) == (0.0, 0.0, 0.0)


def test_position_matters_when_one_catalog_must_be_first() -> None:
    right = score_case(case((A, B), first=A), [A, B])
    wrong = score_case(case((A, B), first=A), [B, A])

    assert right is not None
    assert wrong is not None
    assert right.score == 1.0
    # Both came back, so recall and precision are perfect; only the order is wrong.
    assert (wrong.recall, wrong.precision) == (1.0, 1.0)
    assert wrong.score < 1.0
    # gains {A: 2, B: 1}; found [B, A] = 1 + 2/log2(3); ideal = 2 + 1/log2(3)
    expected = (1 + 2 / math.log2(3)) / (2 + 1 / math.log2(3))
    assert wrong.ndcg == pytest.approx(expected)


def test_without_a_required_order_either_order_is_the_ideal() -> None:
    swapped = score_case(case((A, B)), [B, A])
    assert swapped is not None
    assert swapped.score == 1.0


def test_an_ordered_case_rewards_the_expected_order_in_full() -> None:
    right = score_case(case((A, B, C), ordered=True), [A, B, C])
    wrong = score_case(case((A, B, C), ordered=True), [C, B, A])

    assert right is not None
    assert wrong is not None
    assert right.score == 1.0
    assert 0 < wrong.score < 1.0


def test_a_search_whose_ideal_is_nothing_scores_one_for_nothing_and_zero_for_anything() -> None:
    nothing = score_case(case(()), [])
    something = score_case(case(()), [A])

    assert nothing is not None
    assert something is not None
    assert nothing.score == 1.0
    assert something.score == 0.0
    assert nothing.recall is None
    assert nothing.precision is None


def test_a_case_with_no_judgement_is_not_scored() -> None:
    assert score_case(case(None), [A, B]) is None


def test_only_the_first_ten_results_count_toward_ndcg() -> None:
    noise = [f"n{i}" for i in range(12)]
    late = score_case(case((A,)), [*noise, A])
    early = score_case(case((A,)), [A, *noise])

    assert late is not None
    assert early is not None
    assert late.ndcg == 0.0
    assert early.ndcg == 1.0


# --- all searches ----------------------------------------------------------------------------


def test_the_summary_is_the_mean_over_scored_cases_only() -> None:
    cases = [
        Case("s", "one", "", (A,), []),
        Case("s", "two", "", (A,), []),
        Case("s", "three", "", None, []),
        Case("s", "four", "", (), []),
    ]
    scored = score_all({"one": [A], "two": [B], "three": [A, B, C], "four": []}, cases)

    summary = summarise(scored)

    assert summary.cases == 3
    assert summary.perfect == 2
    assert summary.mean_score == pytest.approx(2 / 3)
    # Recall and precision leave out the search whose ideal is nothing.
    assert summary.mean_recall == 0.5
    assert summary.mean_precision == 0.5


# --- the cases the score reads ---------------------------------------------------------------

CATALOGS = set(titles_in(SEED_FILE) + titles_in(LIBRARIES_FILE))


def test_every_catalog_a_case_names_is_in_a_data_file() -> None:
    """A renamed catalog would otherwise turn an expectation into a silent zero."""
    named = {title for c in QUALITY_CASES for title in (c.expected or ())}
    named |= {title for c in QUALITY_CASES for title, _tier in c.today}
    named |= {c.first for c in QUALITY_CASES if c.first}

    assert named - CATALOGS == set()


def test_a_case_that_pins_a_first_catalog_expects_it() -> None:
    for c in QUALITY_CASES:
        assert c.first is None or c.first in (c.expected or ()), c.query


def test_no_search_appears_twice() -> None:
    queries = [c.query for c in QUALITY_CASES]

    assert len(queries) == len(set(queries))


def test_every_case_says_what_it_checks_except_the_empty_ones_that_say_so_themselves() -> None:
    assert all(c.checks.strip() for c in QUALITY_CASES)


def test_today_lists_word_matches_before_trigram_matches() -> None:
    for c in QUALITY_CASES:
        tiers = [tier for _title, tier in c.today]
        assert tiers == sorted(tiers), c.query


def test_the_score_of_todays_results_is_pinned() -> None:
    """The headline number. A tweak that moves search moves today's results, which moves this:
    update it deliberately, together with the page (`make search-cases`)."""
    summary = summarise(score_all(today_titles()))

    assert round(summary.mean_score, 3) == 0.902
    assert (summary.cases, summary.perfect) == (61, 51)


# --- the page --------------------------------------------------------------------------------


def test_the_test_case_page_is_up_to_date() -> None:
    """Generated from the same data as the tests, so it cannot say something they do not."""
    page = (REPO_ROOT / "docs" / "search-test-cases.md").read_text(encoding="utf-8")

    assert page == render_cases_markdown(), "run `make search-cases` and commit the result"


def test_the_page_lists_every_search_with_its_score() -> None:
    page = render_cases_markdown()

    for c in QUALITY_CASES:
        if c.query.strip():
            assert f"`{c.query}`".replace("|", "\\|") in page, c.query
    assert "| Search | What it checks | Expected | Today | Score | Note |" in page
    assert "BnParis" in page


# --- the command -----------------------------------------------------------------------------


def fake_fetch(results: dict[str, list[str]]) -> object:
    return lambda _base, query: results[query]


def test_the_command_scores_a_server_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(score_search, "fetch_search_titles", fake_fetch(today_titles()))

    status = score_search.main(["--url", "http://example.test"])

    out = capsys.readouterr().out
    assert status == 0
    assert "score 0.902" in out
    assert "perfect 51/61" in out
    assert "Île de France" in out, "an imperfect search is listed with what it wanted and got"


def test_the_command_fails_below_a_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(score_search, "fetch_search_titles", fake_fetch(today_titles()))

    assert score_search.main(["--min-score", "0.95"]) == 1
    assert "below the floor" in capsys.readouterr().out
    assert score_search.main(["--min-score", "0.9"]) == 0


def test_a_baseline_shows_a_tweak_as_better_worse_or_unchanged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    baseline = tmp_path / "before.json"
    monkeypatch.setattr(score_search, "fetch_search_titles", fake_fetch(today_titles()))
    assert score_search.main(["--save", str(baseline)]) == 0
    assert json.loads(baseline.read_text(encoding="utf-8"))["mean"] == pytest.approx(
        0.902, abs=1e-3
    )

    capsys.readouterr()
    assert score_search.main(["--baseline", str(baseline)]) == 0
    assert "unchanged" in capsys.readouterr().out

    worse = today_titles()
    worse["Paris"] = []  # the best-known search stops finding anything
    monkeypatch.setattr(score_search, "fetch_search_titles", fake_fetch(worse))
    assert score_search.main(["--baseline", str(baseline)]) == 1
    out = capsys.readouterr().out
    assert "worse" in out
    assert "-1.00" in out, "the search that got worse is shown with its change"

    better = today_titles()
    better["Île de France"] = ["Bibliothèque numérique de Paris"]  # the noise is gone
    monkeypatch.setattr(score_search, "fetch_search_titles", fake_fetch(better))
    assert score_search.main(["--baseline", str(baseline)]) == 0
    assert "better" in capsys.readouterr().out


def test_an_unreachable_server_is_reported_not_scored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert score_search.main(["--url", "http://127.0.0.1:9"]) == 2
    assert "is not answering" in capsys.readouterr().out
