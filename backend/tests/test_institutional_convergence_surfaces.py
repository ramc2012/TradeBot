"""Trade surfaces for the institutional-convergence paper books:
order log, closed-trade book, open-position detail, and statistics math."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import institutional_convergence as router_module
from institutional_convergence.paper import ConvergencePaperBook
from institutional_convergence.stats import (
    compute_statistics,
    initial_risk_amount,
    open_position_detail,
    trade_records,
)
from paper_engine.base_strategy_agent import IST


def _closed(symbol, pnl, *, reason="hard_stop", session="2026-07-13", entry=100.0,
            stop=95.0, lots=2, lot_size=10, opened="2026-07-13T10:00:00+05:30",
            closed="2026-07-13T10:30:00+05:30"):
    return {
        "position_id": f"IC-{symbol}-1",
        "symbol": symbol, "direction": "LONG", "entry_price": entry,
        "initial_stop": stop, "stop": stop, "lot_size": lot_size,
        "lots": lots, "initial_lots": lots, "realized_pnl": pnl,
        "exit_price": entry + pnl / (lots * lot_size), "exit_reason": reason,
        "session_date": session, "opened_at": opened, "closed_at": closed,
        "status": "closed",
    }


# ── Statistics math (pure functions) ───────────────────────────────────────


def test_compute_statistics_core_ratios() -> None:
    closed = [
        _closed("A", 200.0),                       # win, risk=5*10*2=100 -> R=+2
        _closed("B", -100.0),                      # loss -> R=-1
        _closed("C", 300.0, reason="target1"),     # win -> R=+3
        _closed("D", -100.0, session="2026-07-14",
                opened="2026-07-14T10:00:00+05:30", closed="2026-07-14T11:00:00+05:30"),
    ]

    stats = compute_statistics(closed, initial_capital=1_000_000.0)

    assert stats["trade_count"] == 4
    assert stats["wins"] == 2 and stats["losses"] == 2
    assert stats["win_rate"] == 0.5
    assert stats["gross_profit"] == 500.0
    assert stats["gross_loss"] == 200.0
    assert stats["net_pnl"] == 300.0
    assert stats["profit_factor"] == 2.5
    assert stats["expectancy"] == 75.0
    assert stats["avg_r"] == 0.75  # (2 - 1 + 3 - 1) / 4
    assert stats["r_sample_size"] == 4
    assert stats["avg_win"] == 250.0
    assert stats["avg_loss"] == -100.0


def test_compute_statistics_max_drawdown_from_equity_curve() -> None:
    # Ordered by closed_at: +500, -300, -300 (trough dd=600 off peak), +1000.
    times = [f"2026-07-13T1{i}:00:00+05:30" for i in range(4)]
    closed = [
        _closed("A", 500.0, closed=times[0]),
        _closed("B", -300.0, closed=times[1]),
        _closed("C", -300.0, closed=times[2]),
        _closed("D", 1000.0, closed=times[3]),
    ]

    stats = compute_statistics(closed, initial_capital=10_000.0)

    assert stats["max_drawdown"] == 600.0
    assert round(stats["max_drawdown_pct"], 4) == round(600.0 / 10_500.0 * 100.0, 4)


def test_compute_statistics_breakdowns_and_daily_series() -> None:
    closed = [
        _closed("A", 200.0, reason="target1"),
        _closed("A", -100.0, reason="hard_stop"),
        _closed("B", 300.0, reason="cvd_reversal", session="2026-07-14",
                opened="2026-07-14T10:00:00+05:30", closed="2026-07-14T10:30:00+05:30"),
    ]

    stats = compute_statistics(closed)

    assert stats["per_symbol"]["A"] == {
        "trades": 2, "wins": 1, "losses": 1, "win_rate": 0.5, "pnl": 100.0, "avg_r": 0.5,
    }
    assert stats["per_exit_reason"]["cvd_reversal"]["pnl"] == 300.0
    assert [row["date"] for row in stats["daily_pnl"]] == ["2026-07-13", "2026-07-14"]
    assert stats["daily_pnl"][0] == {"date": "2026-07-13", "pnl": 100.0, "trades": 2, "wins": 1, "cumulative_pnl": 100.0}
    assert stats["daily_pnl"][1]["cumulative_pnl"] == 400.0


def test_compute_statistics_empty_book_is_all_none_or_zero() -> None:
    stats = compute_statistics([])

    assert stats["trade_count"] == 0
    assert stats["win_rate"] is None
    assert stats["profit_factor"] is None
    assert stats["expectancy"] is None
    assert stats["avg_r"] is None
    assert stats["max_drawdown"] == 0.0
    assert stats["daily_pnl"] == []


def test_profit_factor_none_when_no_losses() -> None:
    stats = compute_statistics([_closed("A", 200.0)])

    assert stats["profit_factor"] is None
    assert stats["win_rate"] == 1.0


def test_trade_records_duration_and_r_multiple() -> None:
    rows = trade_records([_closed("A", 200.0)])

    assert len(rows) == 1
    row = rows[0]
    assert row["duration_minutes"] == 30.0
    assert row["r_multiple"] == 2.0
    assert row["pnl"] == 200.0
    assert row["exit_reason"] == "hard_stop"
    # CSV-able: every value is a scalar.
    assert all(not isinstance(value, (dict, list)) for value in row.values())


def test_legacy_position_without_initial_stop_degrades_to_none_r() -> None:
    """Pre-surface positions moved stop to break-even: risk collapses to 0 and
    the R-multiple must be None, not a divide-by-zero or a lie."""
    legacy = _closed("A", 200.0)
    del legacy["initial_stop"]
    legacy["stop"] = legacy["entry_price"]  # break-even move already happened

    assert initial_risk_amount(legacy) == 0.0
    row = trade_records([legacy])[0]
    assert row["r_multiple"] is None
    stats = compute_statistics([legacy])
    assert stats["avg_r"] is None and stats["r_sample_size"] == 0
    assert stats["net_pnl"] == 200.0  # pnl math unaffected


def test_open_position_detail_distances_r_and_age() -> None:
    now = datetime(2026, 7, 13, 11, 0, tzinfo=IST)
    position = {
        "symbol": "NIFTY", "direction": "LONG", "entry_price": 100.0,
        "current_price": 104.0, "stop": 100.0, "initial_stop": 95.0,
        "target1": 105.0, "target2": 110.0, "lot_size": 10, "lots": 1,
        "initial_lots": 2, "realized_pnl": 50.0,
        "opened_at": "2026-07-13T10:00:00+05:30",
    }

    detail = open_position_detail(position, now)

    assert detail["unrealized_pnl"] == 40.0            # (104-100)*10*1
    assert detail["total_pnl"] == 90.0
    assert detail["initial_risk_amount"] == 100.0      # |100-95|*10*2
    assert detail["r_multiple"] == 0.9
    assert detail["age_minutes"] == 60.0
    assert detail["stop_distance"] == 4.0              # buffer above BE stop
    assert detail["target1_distance"] == 1.0           # left to travel
    assert detail["target2_distance"] == 6.0
    assert detail["stop_distance_pct"] == round(4.0 / 104.0 * 100.0, 4)


def test_open_position_detail_short_side_signs() -> None:
    now = datetime(2026, 7, 13, 11, 0, tzinfo=IST)
    position = {
        "symbol": "GOLD", "direction": "SHORT", "entry_price": 100.0,
        "current_price": 98.0, "stop": 103.0, "initial_stop": 103.0,
        "target1": 94.0, "target2": None, "lot_size": 10, "lots": 2,
        "initial_lots": 2, "realized_pnl": 0.0,
        "opened_at": "2026-07-13T10:30:00+05:30",
    }

    detail = open_position_detail(position, now)

    assert detail["unrealized_pnl"] == 40.0            # (100-98)*10*2
    assert detail["stop_distance"] == 5.0              # 103-98 adverse buffer
    assert detail["target1_distance"] == 4.0           # 98-94 still to travel
    assert detail["target2_distance"] is None
    assert detail["r_multiple"] == round(40.0 / 60.0, 3)


# ── Order log in the paper book ────────────────────────────────────────────


def _signal(symbol="NIFTY", spot=100.0):
    return {
        "symbol": symbol, "status": "actionable_paper", "action": "LONG", "spot": spot,
        "risk": {"entry": 100.0, "stop": 90.0, "target1": 110.0, "target2_long": 120.0,
                 "lot_size": 50, "risk_fraction": 0.01},
        "cvd": {"series": [{"cvd": 1}, {"cvd": 2}]},
    }


def test_order_log_records_open_partial_and_close(tmp_path) -> None:
    book = ConvergencePaperBook(tmp_path / "paper.json")
    now = datetime(2026, 7, 13, 10, 30, tzinfo=IST)
    signal = _signal()

    book.sync([signal], now)                       # open 20 lots
    signal["spot"] = 110.0
    book.sync([signal], now.replace(minute=33))    # target1: book half, BE stop
    signal["spot"] = 99.0
    book.sync([signal], now.replace(minute=36))    # BE stop hit -> hard_stop

    log = book.orders()["orders"]
    assert [row["action"] for row in log] == ["open", "partial_close", "close"]
    opened, partial, closed = log
    assert opened["symbol"] == "NIFTY" and opened["price"] == 100.0 and opened["lots"] == 20
    assert opened["reason"] == "signal_entry"
    assert partial["lots"] == 10 and partial["lots_remaining"] == 10
    assert partial["price"] == 110.0 and partial["pnl"] == 5000.0
    assert closed["reason"] == "hard_stop" and closed["lots"] == 10
    assert closed["pnl"] == -500.0                 # (99-100)*50*10
    # All three rows share the position id -> reconstructable lifecycle.
    assert len({row["position_id"] for row in log}) == 1
    # Position book agrees with the log.
    assert book.trades()["trades"][0]["pnl"] == 4500.0


def test_order_log_survives_legacy_state_file_without_order_log(tmp_path) -> None:
    book = ConvergencePaperBook(tmp_path / "paper.json")
    # Legacy state: no order_log key, position without initial_stop.
    book._save({
        "initial_capital": 1_000_000,
        "open_positions": [{
            "position_id": "IC-NIFTY-legacy", "symbol": "NIFTY", "direction": "LONG",
            "entry_price": 100.0, "current_price": 100.0, "stop": 95.0,
            "target1": 110.0, "target2": None, "lot_size": 50, "lots": 4,
            "initial_lots": 4, "target1_done": False, "realized_pnl": 0.0,
            "opened_at": "2026-07-13T10:00:00+05:30", "session_date": "2026-07-13",
            "status": "open",
        }],
        "closed_positions": [],
    })

    result = book.sync([_signal(spot=94.0)], datetime(2026, 7, 13, 11, 0, tzinfo=IST))

    assert result["open_count"] == 0  # legacy position stopped out cleanly
    log = book.orders()["orders"]
    assert [row["action"] for row in log] == ["close"]
    assert log[0]["position_id"] == "IC-NIFTY-legacy"
    assert log[0]["reason"] == "hard_stop"


def test_order_log_is_capped(tmp_path) -> None:
    from institutional_convergence import paper as paper_module

    book = ConvergencePaperBook(tmp_path / "paper.json")
    book._save({
        "initial_capital": 1_000_000, "open_positions": [], "closed_positions": [],
        "order_log": [{"action": "close", "position_id": f"IC-{i}"} for i in range(paper_module.ORDER_LOG_LIMIT + 50)],
    })

    book.sync([], datetime(2026, 7, 13, 11, 0, tzinfo=IST))

    assert book.orders(limit=paper_module.ORDER_LOG_LIMIT * 2)["count"] == paper_module.ORDER_LOG_LIMIT


def test_consumed_setups_trim_evicts_oldest_not_alphabetical(tmp_path) -> None:
    """The 500-id retention trim must evict by AGE (insertion order). A
    sorted() trim would drop alphabetically-first symbols' ids — including
    today's — while retaining months-old late-alphabet ids, re-enabling entry
    on an already-consumed setup."""
    book = ConvergencePaperBook(tmp_path / "paper.json")
    stale = [f"ZZZ:LONG:2026-01-01T{i:02d}:{j:02d}:00" for i in range(20) for j in range(25)]
    book._save({
        "initial_capital": 1_000_000, "open_positions": [], "closed_positions": [],
        "consumed_setups": stale,  # exactly 500 — the cap is full
    })
    signal = _signal(symbol="AAA")
    signal["long_setup"] = {"bar_time": "2026-07-13T10:27:00+05:30"}

    book.sync([signal], datetime(2026, 7, 13, 10, 30, tzinfo=IST))

    import json
    persisted = json.loads((tmp_path / "paper.json").read_text())
    kept = persisted["consumed_setups"]
    assert len(kept) == 500
    # Today's AAA id must survive the trim (it is the NEWEST)…
    assert kept[-1] == "AAA:LONG:2026-07-13T10:27:00+05:30"
    # …and the evicted id is the OLDEST stale one, not the alphabetical first.
    assert stale[0] not in kept
    assert kept[:2] == stale[1:3]


def test_summary_enriches_open_positions_without_persisting(tmp_path) -> None:
    book = ConvergencePaperBook(tmp_path / "paper.json")
    now = datetime(2026, 7, 13, 10, 30, tzinfo=IST)

    summary = book.sync([_signal()], now)

    position = summary["open_positions"][0]
    assert position["initial_stop"] == 90.0
    assert "unrealized_pnl" in position and "r_multiple" in position
    assert "stop_distance" in position and "age_minutes" in position
    # Derived fields must never be written into the state file.
    import json
    persisted = json.loads((tmp_path / "paper.json").read_text())
    assert "unrealized_pnl" not in persisted["open_positions"][0]
    assert "stop_distance" not in persisted["open_positions"][0]
    assert persisted["order_log"][0]["action"] == "open"


# ── Endpoint payload shapes ────────────────────────────────────────────────


def _client_with_book(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(router_module.convergence_paper_book, "path", tmp_path / "paper.json")
    monkeypatch.setattr(router_module.commodity_convergence_paper_book, "path", tmp_path / "commodity_paper.json")
    app = FastAPI()
    app.include_router(router_module.router)
    return TestClient(app)


def _run_one_trade(book: ConvergencePaperBook) -> None:
    now = datetime(2026, 7, 13, 10, 30, tzinfo=IST)
    signal = _signal()
    book.sync([signal], now)
    signal["spot"] = 89.0
    book.sync([signal], now + timedelta(minutes=3))


def test_trades_orders_statistics_endpoints_shapes(tmp_path, monkeypatch) -> None:
    client = _client_with_book(tmp_path, monkeypatch)
    _run_one_trade(router_module.convergence_paper_book)

    trades = client.get("/api/institutional-convergence/trades").json()
    assert trades["count"] == 1
    row = trades["trades"][0]
    assert {"position_id", "symbol", "direction", "entry_price", "exit_price",
            "exit_reason", "pnl", "r_multiple", "duration_minutes",
            "opened_at", "closed_at", "lots", "lot_size"} <= set(row)
    assert row["exit_reason"] == "hard_stop"
    assert row["duration_minutes"] == 3.0

    orders = client.get("/api/institutional-convergence/orders").json()
    assert orders["count"] == 2
    assert [entry["action"] for entry in orders["orders"]] == ["open", "close"]
    limited = client.get("/api/institutional-convergence/orders", params={"limit": 1}).json()
    assert len(limited["orders"]) == 1 and limited["count"] == 2

    stats = client.get("/api/institutional-convergence/statistics").json()
    assert stats["trade_count"] == 1
    assert stats["losses"] == 1
    assert {"win_rate", "profit_factor", "expectancy", "avg_r", "max_drawdown",
            "per_symbol", "per_exit_reason", "daily_pnl", "updated_at"} <= set(stats)
    assert "NIFTY" in stats["per_symbol"]
    assert "hard_stop" in stats["per_exit_reason"]


def test_commodity_variant_endpoints_are_isolated(tmp_path, monkeypatch) -> None:
    client = _client_with_book(tmp_path, monkeypatch)
    _run_one_trade(router_module.convergence_paper_book)

    # NSE book has the trade; the commodity book must stay empty.
    assert client.get("/api/institutional-convergence/trades").json()["count"] == 1
    commodity_trades = client.get("/api/institutional-convergence/commodity/trades").json()
    assert commodity_trades == {"trades": [], "count": 0, "updated_at": None}
    commodity_stats = client.get("/api/institutional-convergence/commodity/statistics").json()
    assert commodity_stats["trade_count"] == 0
    assert client.get("/api/institutional-convergence/commodity/orders").json()["orders"] == []
