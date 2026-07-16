"""F-18 persist-state payload trim (2026-07-15).

The `app_runtime_state` JSONB blobs grow DAILY (an equity point per scan, a
full row per closed trade) and are json-decoded ON the event loop on every
restore — py-spy caught MainThread seized decoding a giant blob, the prime
"backend stales later each day" mechanism. Persisted payloads are now bounded:

  * equity curve  → last 2000 points
  * recent events → last 200
  * trade history → last 500 rows verbatim + rolling aggregate summary

These tests pin the trim helpers (shared in base_strategy_agent), the summary
fold semantics (idempotent — repeated persists never double-count), the
backward-readability of trimmed payloads, and the commodity agent's
normalize/apply/persist roundtrip plus its new to_thread persist.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

from paper_engine.base_strategy_agent import (
    IST,
    PERSIST_EQUITY_CURVE_MAX_POINTS,
    PERSIST_TRADE_HISTORY_MAX_ROWS,
    _deserialize_trade_history,
    _restore_archived_trade_summary,
    _serialize_equity_curve,
    _summarize_trade_rows,
    _trade_history_persist_payload,
)
from paper_engine.portfolio import PaperPortfolio, TradeRecord


def _mk_portfolio(*, trades: int = 0, curve_points: int = 0) -> PaperPortfolio:
    portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id="trim-test")
    t0 = datetime(2026, 7, 1, 9, 30, tzinfo=IST)
    portfolio._trade_history = [
        TradeRecord(
            symbol=f"SYM{i}",
            action="BUY",
            qty=10,
            entry_price=100.0,
            exit_price=101.0 if i % 2 == 0 else 99.0,
            pnl=10.0 if i % 2 == 0 else -10.0,
            entry_time=t0 + timedelta(minutes=i),
            exit_time=t0 + timedelta(minutes=i, seconds=30),
        )
        for i in range(trades)
    ]
    portfolio._equity_curve = [
        (datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(minutes=i), 1_000_000.0 + i)
        for i in range(curve_points)
    ]
    return portfolio


def test_equity_curve_persist_keeps_only_the_last_2000_points() -> None:
    portfolio = _mk_portfolio(curve_points=3000)
    rows = _serialize_equity_curve(portfolio)
    assert len(rows) == PERSIST_EQUITY_CURVE_MAX_POINTS
    # The LAST points survive (the tail is what the UI/restore needs).
    assert rows[-1]["equity"] == 1_000_000.0 + 2999
    assert rows[0]["equity"] == 1_000_000.0 + 1000
    # Under the bound → unchanged behavior.
    small = _mk_portfolio(curve_points=5)
    assert len(_serialize_equity_curve(small)) == 5


def test_trade_history_under_bound_passes_through_with_no_summary() -> None:
    portfolio = _mk_portfolio(trades=10)
    rows, summary = _trade_history_persist_payload(portfolio)
    assert len(rows) == 10
    assert summary is None


def test_trade_history_overflow_is_trimmed_and_summarized() -> None:
    portfolio = _mk_portfolio(trades=620)
    rows, summary = _trade_history_persist_payload(portfolio)
    assert len(rows) == PERSIST_TRADE_HISTORY_MAX_ROWS
    # The MOST RECENT 500 survive verbatim.
    assert rows[0]["symbol"] == "SYM120"
    assert rows[-1]["symbol"] == "SYM619"
    # The oldest 120 are folded into the aggregate.
    assert summary is not None
    assert summary["trades"] == 120
    assert summary["wins"] == 60
    assert summary["losses"] == 60
    assert summary["pnl"] == 0.0
    assert summary["first_entry_time"].startswith("2026-07-01T09:30")
    # Trimmed rows stay fully readable by the existing deserializer.
    assert len(_deserialize_trade_history(rows)) == PERSIST_TRADE_HISTORY_MAX_ROWS


def test_summary_folds_restored_base_and_repeated_persists_never_double_count() -> None:
    portfolio = _mk_portfolio(trades=620)
    # Simulate a payload restored from a previous trim cycle.
    _restore_archived_trade_summary(
        portfolio,
        {"trade_history_summary": {"trades": 40, "wins": 30, "losses": 10, "pnl": 500.0}},
    )
    rows1, summary1 = _trade_history_persist_payload(portfolio)
    assert summary1["trades"] == 160          # 40 archived + 120 new overflow
    assert summary1["pnl"] == 500.0           # overflow nets to 0 here
    # A second persist with unchanged state must produce the SAME summary —
    # the fold is recomputed from the immutable base, never accumulated.
    rows2, summary2 = _trade_history_persist_payload(portfolio)
    assert rows2 == rows1
    assert summary2 == summary1


def test_restore_archived_trade_summary_tolerates_old_payloads() -> None:
    portfolio = _mk_portfolio()
    _restore_archived_trade_summary(portfolio, {})  # pre-trim payload: no key
    assert portfolio._archived_trade_summary is None
    _restore_archived_trade_summary(portfolio, {"trade_history_summary": "junk"})
    assert portfolio._archived_trade_summary is None


def test_summarize_trade_rows_tracks_bounds_and_prior() -> None:
    rows = [
        {"pnl": 5.0, "entry_time": "2026-07-02T10:00:00+05:30", "exit_time": "2026-07-02T10:30:00+05:30"},
        {"pnl": -3.0, "entry_time": "2026-07-01T10:00:00+05:30", "exit_time": "2026-07-03T10:00:00+05:30"},
        {"pnl": 0.0, "entry_time": None, "exit_time": None},
    ]
    summary = _summarize_trade_rows(rows)
    assert summary["trades"] == 3
    assert summary["wins"] == 1 and summary["losses"] == 1
    assert summary["pnl"] == 2.0
    assert summary["first_entry_time"] == "2026-07-01T10:00:00+05:30"
    assert summary["last_exit_time"] == "2026-07-03T10:00:00+05:30"
    folded = _summarize_trade_rows(rows, prior=summary)
    assert folded["trades"] == 6 and folded["pnl"] == 4.0


def test_s1_runtime_state_payload_is_bounded() -> None:
    from paper_engine.strategy_agent import PaperStrategyAgent
    from paper_engine.strategy_agent_state import StrategyRuntime
    from paper_engine.order_book import PaperOrderBook

    portfolio = _mk_portfolio(trades=620, curve_points=3000)
    runtime = StrategyRuntime(
        key="strategy1",
        label="S1",
        portfolio=portfolio,
        order_book=PaperOrderBook(on_fill=portfolio.on_fill),
    )
    payload = PaperStrategyAgent._serialize_runtime_state(object(), runtime)
    assert len(payload["portfolio"]["trade_history"]) == PERSIST_TRADE_HISTORY_MAX_ROWS
    assert payload["portfolio"]["trade_history_summary"]["trades"] == 120
    assert len(payload["portfolio"]["equity_curve"]) == PERSIST_EQUITY_CURVE_MAX_POINTS


def _commodity_agent(monkeypatch, tmp_path):
    import paper_engine.commodity_strategy_agent as csa

    store: dict[str, object] = {"payload": None, "updated_at": None}

    def _load_runtime_state(_key):
        return store["payload"], store["updated_at"]

    def _save_runtime_state(_key, payload):
        store["payload"] = payload
        store["updated_at"] = datetime.now(timezone.utc)
        return store["updated_at"]

    monkeypatch.setattr(csa, "load_runtime_state", _load_runtime_state)
    monkeypatch.setattr(csa, "save_runtime_state", _save_runtime_state)
    monkeypatch.setattr(csa, "_COMMODITY_CONFIG_FILE", tmp_path / "commodity_strategy.json")
    return csa, csa.CommodityStrategyAgent(), store


def test_commodity_saved_state_is_bounded_and_summary_survives_normalize(monkeypatch, tmp_path) -> None:
    csa, agent, _store = _commodity_agent(monkeypatch, tmp_path)
    donor = _mk_portfolio(trades=520, curve_points=2500)
    agent._runtime.portfolio._trade_history = donor._trade_history
    agent._runtime.portfolio._equity_curve = donor._equity_curve

    state = agent._build_saved_state()
    portfolio_payload = state["runtime"]["portfolio"]
    assert len(portfolio_payload["trade_history"]) == PERSIST_TRADE_HISTORY_MAX_ROWS
    assert len(portfolio_payload["equity_curve"]) == PERSIST_EQUITY_CURVE_MAX_POINTS
    assert portfolio_payload["trade_history_summary"]["trades"] == 20

    # The summary must survive _normalize_saved_state (the DB-restore path) …
    normalized = csa._normalize_saved_state(state)
    assert normalized["runtime"]["portfolio"]["trade_history_summary"]["trades"] == 20

    # … and land back on the portfolio via _apply_saved_state, so the NEXT
    # persist folds on top of it instead of resetting the archive.
    agent._apply_saved_state(normalized)
    assert agent._runtime.portfolio._archived_trade_summary["trades"] == 20
    state2 = agent._build_saved_state()
    assert state2["runtime"]["portfolio"]["trade_history_summary"]["trades"] == 20


def test_commodity_apersist_state_offloads_to_a_worker_thread(monkeypatch, tmp_path) -> None:
    csa, agent, _store = _commodity_agent(monkeypatch, tmp_path)
    seen: dict[str, object] = {}

    def _fake_save_state(payload):
        seen["thread"] = threading.current_thread()
        seen["payload"] = payload
        return datetime.now(timezone.utc)

    monkeypatch.setattr(csa, "_save_state", _fake_save_state)
    asyncio.run(agent._apersist_state())
    assert seen["thread"] is not threading.main_thread()
    assert isinstance(seen["payload"], dict)
    assert agent._state_synced_at is not None


def test_realized_pnl_and_reconcile_fold_the_archived_summary() -> None:
    # The F-18 trim archives trades OUT of the persisted payload; their P&L
    # must keep counting toward realized_pnl or reconcile_available_capital()
    # (called on every dashboard get_summary and on commodity restore) would
    # erase the archived trades' cash.
    portfolio = _mk_portfolio(trades=4)  # +10 −10 +10 −10 → nets 0
    assert portfolio.realized_pnl == 0.0
    portfolio._archived_trade_summary = {"trades": 7, "wins": 5, "losses": 2, "pnl": 123.5}
    assert portfolio.realized_pnl == 123.5
    portfolio.reconcile_available_capital()
    assert portfolio.available_capital == 1_000_000.0 + 123.5
    # Junk archive shapes must degrade to 0, never raise.
    portfolio._archived_trade_summary = {"pnl": "not-a-number"}
    assert portfolio.realized_pnl == 0.0


def test_commodity_old_shape_payload_roundtrip_preserves_capital_and_daily_pnl(
    monkeypatch, tmp_path
) -> None:
    # ADVERSARIAL backward-compat: restore a PRE-TRIM payload (full 600-row
    # trade_history, NO trade_history_summary key), persist (which trims), and
    # restore again — lifetime realized P&L, reconciled cash, and archived
    # days' daily_pnl buckets must all survive the trim cycle.
    from paper_engine.base_strategy_agent import _serialize_trade_history

    csa, agent, _store = _commodity_agent(monkeypatch, tmp_path)

    donor = PaperPortfolio(initial_capital=5_000_000.0, session_id="old-shape")
    t0 = datetime(2026, 6, 1, 10, 0, tzinfo=IST)
    donor._trade_history = [
        TradeRecord(
            symbol=f"SYM{i}",
            action="BUY",
            qty=1,
            entry_price=100.0,
            exit_price=100.0 + i,
            pnl=float(i),
            entry_time=t0 + timedelta(days=i // 50, minutes=i % 50),
            exit_time=t0 + timedelta(days=i // 50, minutes=i % 50, seconds=30),
        )
        for i in range(600)
    ]
    full_pnl = sum(t.pnl for t in donor._trade_history)  # 179700.0

    # Build a payload in the OLD persist shape: full history, no summary key.
    state = agent._build_saved_state()
    state["runtime"]["portfolio"]["trade_history"] = _serialize_trade_history(donor)
    state["runtime"]["portfolio"].pop("trade_history_summary", None)
    state["runtime"]["portfolio"]["daily_pnl"] = {}
    state["runtime"]["portfolio"]["available_capital"] = 5_000_000.0 + full_pnl

    agent._apply_saved_state(csa._normalize_saved_state(state))
    portfolio = agent._runtime.portfolio
    assert portfolio.realized_pnl == full_pnl
    assert portfolio.available_capital == portfolio.initial_capital + full_pnl

    # Persist → trims to 500 rows + summary of the oldest 100 (pnl 0..99).
    state2 = agent._build_saved_state()
    pp = state2["runtime"]["portfolio"]
    assert len(pp["trade_history"]) == PERSIST_TRADE_HISTORY_MAX_ROWS
    assert pp["trade_history_summary"]["trades"] == 100
    assert pp["trade_history_summary"]["pnl"] == float(sum(range(100)))

    # Second restore (the post-restart path, incl. _repair_portfolio_ledger's
    # reconcile): nothing about lifetime accounting may drift.
    agent._apply_saved_state(csa._normalize_saved_state(state2))
    portfolio = agent._runtime.portfolio
    assert portfolio.realized_pnl == full_pnl
    assert portfolio.available_capital == portfolio.initial_capital + full_pnl
    # Archived days (trades 0..99 → the first two exit dates) keep their
    # daily buckets even though their rows left the payload.
    day0 = (t0 + timedelta(days=0)).date()
    day1 = (t0 + timedelta(days=1)).date()
    assert portfolio._daily_pnl[day0] == float(sum(range(50)))
    assert portfolio._daily_pnl[day1] == float(sum(range(50, 100)))
    # And a third persist keeps folding instead of double counting.
    state3 = agent._build_saved_state()
    assert state3["runtime"]["portfolio"]["trade_history_summary"]["trades"] == 100
    assert state3["runtime"]["portfolio"]["trade_history_summary"]["pnl"] == float(sum(range(100)))
