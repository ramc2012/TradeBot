from datetime import date, datetime, timezone
from pathlib import Path
import pandas as pd

from features import m2_flow
from fusion import m6_select


def test_emit_session_preserves_the_report_accumulator(monkeypatch):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,*args): pass
        def fetchone(self): return (datetime(2026,8,31,9,45,tzinfo=timezone.utc),)
    class Connection:
        def cursor(self): return Cursor()
    monkeypatch.setattr(m2_flow,"upsert_features",lambda connection,rows: len(rows))
    report = []
    assert m2_flow._emit_session(Connection(),["TEST"],date(2026,8,31),
        pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),report) == 1
    assert len(report) == 1 and "ivs_z" in report[0]
    assert report[0]["ts"] == datetime(2026,8,31,9,45,tzinfo=timezone.utc)
    assert m2_flow._emit_session(Connection(),["TEST"],date(2026,8,31),
        pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),None) == 1


def test_latest_evaluable_bar_has_completion_cutoff():
    assert "<= %(now)s" in m6_select.LATEST_BAR_SQL
    assert "interval '30 minutes'" in m6_select.LATEST_BAR_SQL
    assert "15:30" in m6_select.LATEST_BAR_SQL


def test_inference_query_cannot_fall_back_to_an_old_option_bar():
    import inspect
    source = inspect.getsource(m6_select.resolve_instruments_at)
    assert "o.time = %(ts)s" in source
    assert "interval '2 days'" not in source
    assert '"source_mark_ts": row["time"]' in source


def test_new_predictions_are_immutable_and_legacy_outcomes_are_not_relabelled():
    """Prediction immutability moved with the writer.

    The comparator's forecasts used to be written as a side effect of
    persist_tickets, because the model owned the ticket path. It no longer
    does (2026-09-04 plan), so the same ON CONFLICT guard is asserted where
    the predictions are actually written now.
    """
    import inspect
    persist = inspect.getsource(m6_select.persist_model_diagnostics)
    assert "ON CONFLICT (ts, symbol, option_type, model_version) DO NOTHING" in persist
    assert "INSERT INTO vanguard_model_predictions" not in inspect.getsource(
        m6_select.persist_tickets)
    resolve = inspect.getsource(m6_select.resolve_model_prediction_outcomes)
    assert "p.source_mark_ts=p.ts" in resolve
    assert "p.timing_policy='completed_same_bar_v1'" in resolve


def test_training_never_auto_promotes_or_retires_an_existing_model():
    source = (Path(__file__).parents[1]/"research/train_nonlinear_selector.py").read_text()
    assert 'status = "shadow"' in source
    assert "SET status='retired'" not in source
    from scripts.cycle_daemon import EOD_STEPS
    assert not any("train_nonlinear_selector.py" in " ".join(args) for _,args in EOD_STEPS)
