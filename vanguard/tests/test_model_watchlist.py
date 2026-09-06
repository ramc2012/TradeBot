import pytest

from model.watchlist import performance, rank_candidates


def test_rank_candidates_observes_all_rows_deduplicates_and_marks_qualification():
    rows = [
        {"symbol": "B", "option_type": "PE", "conservative_edge": .07,
         "selection_threshold": .03},
        {"symbol": "A", "option_type": "CE", "conservative_edge": .08,
         "selection_threshold": .03},
        {"symbol": "A", "option_type": "PE", "conservative_edge": .06,
         "selection_threshold": .03},
        {"symbol": "C", "option_type": "CE", "conservative_edge": .02,
         "selection_threshold": .03},
    ]

    ranked = rank_candidates(rows, top_n=3)

    assert [(row["rank"], row["symbol"], row["option_type"]) for row in ranked] == [
        (1, "A", "CE"), (2, "B", "PE"), (3, "C", "CE")
    ]
    assert [row["qualified"] for row in ranked] == [True, True, False]


def test_swing_ranking_uses_directional_score_not_intraday_edge():
    rows = [
        {"symbol": "A", "option_type": "CE", "ranking_score": .01,
         "conservative_edge": .20, "selection_threshold": 0},
        {"symbol": "B", "option_type": "PE", "ranking_score": .03,
         "conservative_edge": -.20, "selection_threshold": 0},
    ]
    ranked = rank_candidates(rows, top_n=2)
    assert [row["symbol"] for row in ranked] == ["B", "A"]
    assert [row["qualified"] for row in ranked] == [True, True]


def test_performance_reports_current_mfe_and_mae_from_entry():
    current, maximum, minimum = performance(100.0, 112.0, 125.0, 91.0)
    assert current == pytest.approx(0.12)
    assert maximum == pytest.approx(0.25)
    assert minimum == pytest.approx(-0.09)
