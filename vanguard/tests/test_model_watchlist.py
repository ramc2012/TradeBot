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


def test_late_final_candle_reconciles_missing_outcome_without_rewriting_resolved(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from model import watchlist
    start = datetime(2026, 9, 18, 3, 45, tzinfo=timezone.utc)
    run = {'source_session': start.date()-timedelta(days=1), 'track_session': start.date(),
           'status': 'closed', 'generated_at': start-timedelta(hours=15), 'prediction_ts': start-timedelta(hours=18)}
    items = [{'id': i, 'status': status, 'expiry': start.date()+timedelta(days=10), 'source_mark_ts': None}
             for i, status in [(1, 'missing_contract'), (2, 'closed')]]
    bars = [{'id': 1, 'time': start+timedelta(minutes=30*i), 'open': 100, 'high': 105,
             'low': 99, 'close': 101, 'volume': 1} for i in range(12)]
    writes = []
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return start.replace(hour=16)
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=None):
            if 'SELECT r.*' in sql:
                assert "i.status='missing_contract'" in sql and "TIME '14:45'" in sql
                self.rows = [run]
            elif 'SELECT * FROM vanguard_watchlist_items' in sql: self.rows = items
            elif 'SELECT DISTINCT ON (i.id, o.time)' in sql: self.rows = bars
            elif 'SELECT DISTINCT ON (i.id)' in sql: self.rows = []
            else: writes.append((sql, params)); self.rows = []
        def fetchall(self): return self.rows
    class Connection:
        def cursor(self, **kwargs): return Cursor()
    monkeypatch.setattr(watchlist, 'datetime', Clock)
    monkeypatch.setattr(watchlist, '_register_exit_policy', lambda _: start-timedelta(days=2))
    result = watchlist.track_open_watchlists(Connection())
    assert result['items_updated'] == 1
    item_writes = [(sql, params) for sql, params in writes if 'UPDATE vanguard_watchlist_items' in sql]
    assert all(params[-1] == 1 for _, params in item_writes)
    analysis = item_writes[0][1][0].adapted
    assert analysis['status'] == 'closed'
    assert analysis['latest_ts'].startswith('2026-09-18 09:15')
    assert analysis['reconciliation']['previous_status'] == 'missing_contract'
