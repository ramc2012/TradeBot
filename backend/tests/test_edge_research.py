"""Causality, exact-contract execution, persistence and evidence gates."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import asyncio

import pytest

from edge_research.archive import Archive
from edge_research.engine import ShadowEngine, hypothesis, quote_problem, ts
from edge_research.report import read_report, summarize
from edge_research.sources import aggregate_five, normalize_quote, QuoteReader


CONFIG = json.loads(
    (Path(__file__).parents[1] / "edge_research/frozen_v1.json").read_text()
)
NOW = datetime(2026, 10, 21, 5, 0, 1, tzinfo=timezone.utc)


def frame(now=NOW):
    end = NOW - timedelta(seconds=1)
    bars = []
    for i in range(16):
        t = end - timedelta(minutes=5 * (15 - i))
        close = 99.5 if i < 14 else 101.0 if i == 14 else 102.0
        bars.append(
            {
                "start": (t - timedelta(minutes=5)).isoformat(),
                "end": t.isoformat(),
                "available_at": t.isoformat(),
                "open": close,
                "high": 100.0 if i < 14 else close + 0.3,
                "low": 99.0 if i < 14 else close - 0.3,
                "close": close,
                "volume": 100,
            }
        )
    c = {
        "instrument_key": "NSE_FO|EXACT",
        "trading_symbol": "NIFTY26DEC25000CE",
        "instrument_type": "CE",
        "lot_size": 75,
        "expiry": "2026-12-31",
        "available_at": (NOW - timedelta(seconds=1)).isoformat(),
    }
    return {
        "symbol": "NIFTY",
        "market": "NSE",
        "market_open": True,
        "observed_at": now.isoformat(),
        "bars": bars,
        "contracts": {"CE": c},
    }


def quote(now=NOW, key="NSE_FO|EXACT"):
    return {
        "instrument_key": key,
        "time": (now - timedelta(seconds=1)).isoformat(),
        "observed_at": now.isoformat(),
        "bid": 100.0,
        "ask": 101.0,
        "bid_size": 750,
        "ask_size": 750,
    }


def engine(tmp_path):
    a = Archive(tmp_path / "edge.sqlite3", CONFIG, NOW.isoformat())
    return ShadowEngine(a, CONFIG), a


def test_no_same_quote_fill_and_restart_preserves_pending(tmp_path):
    e, a = engine(tmp_path)
    e.observe(frame(), [quote()])
    assert a.state("directional:challenger:NIFTY")["status"] == "pending"
    a.close()
    e, a = engine(tmp_path)
    later = NOW + timedelta(seconds=15)
    e.observe(frame(later), [quote(later)])
    state = a.state("directional:challenger:NIFTY")
    assert state["status"] == "open" and state["entry_price"] == 101
    assert state["qty"] == 75
    a.close()


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ({"instrument_key": "NSE_FO|OTHER"}, "missing_exact_quote"),
        ({"time": (NOW - timedelta(minutes=3)).isoformat()}, "stale_quote"),
        ({"time": (NOW + timedelta(seconds=1)).isoformat()}, "stale_quote"),
        ({"bid_size": 10}, "insufficient_displayed_size"),
        ({"bid": 0}, "invalid_spread"),
        ({"bid": float("nan")}, "invalid_spread"),
        ({"ask": 110}, "wide_spread"),
        ({"time": None}, "missing_quote_time"),
    ],
)
def test_nonexecutable_quotes_cannot_fill(mutation, reason):
    assert (
        quote_problem({**quote(), **mutation}, frame()["contracts"]["CE"], NOW, CONFIG)
        == reason
    )


def test_entry_expires_without_executable_quote(tmp_path):
    e, a = engine(tmp_path)
    e.observe(frame(), [])
    later = NOW + timedelta(seconds=61)
    e.observe(frame(later), [])
    assert a.state("directional:challenger:NIFTY") is None
    assert a.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert (
        a.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='entry_expired'"
        ).fetchone()[0]
        > 0
    )
    a.close()


def test_net_close_is_on_later_bid_and_stress_is_worse(tmp_path):
    e, a = engine(tmp_path)
    e.observe(frame(), [])
    entered = NOW + timedelta(seconds=15)
    e.observe(frame(entered), [quote(entered)])
    later = NOW + timedelta(minutes=5)
    f = frame(later)
    last = deepcopy(f["bars"][-1])
    last.update(
        start=last["end"],
        end=(NOW + timedelta(minutes=5, seconds=-1)).isoformat(),
        available_at=later.isoformat(),
        open=105.0,
        high=105.5,
        low=104.5,
        close=105.0,
    )
    f["bars"].append(last)
    e.observe(f, [{**quote(later), "bid": 120.0, "ask": 121.0}])
    trades = [json.loads(r[0]) for r in a.db.execute("SELECT payload FROM trades")]
    assert len(trades) == 2
    t = next(t for t in trades if t["variant"] == "challenger")
    assert t["exit_price"] == 120 and t["gross_pnl"] == 1425
    assert t["net_pnl"] == pytest.approx(1425 - t["charges"])
    assert t["stressed_net_pnl"] < t["net_pnl"]
    assert a.state("directional:challenger:NIFTY") is None
    a.close()


@pytest.mark.parametrize("change", ["future", "gap", "invalid"])
def test_bad_candle_inputs_create_no_proposals(tmp_path, change):
    e, a = engine(tmp_path)
    f = frame()
    if change == "future":
        f["bars"][-1]["available_at"] = (NOW + timedelta(minutes=1)).isoformat()
    elif change == "gap":
        f["bars"].pop(5)
    else:
        f["bars"][-1]["high"] = 1
    e.observe(f, [quote()])
    assert a.db.execute("SELECT COUNT(*) FROM states").fetchone()[0] == 0
    a.close()


def test_later_revised_inputs_cannot_rewrite_an_archived_decision(tmp_path):
    e, a = engine(tmp_path)
    e.observe(frame(), [])
    key = f"directional:challenger:NIFTY:{frame()['bars'][-1]['end']}"
    before = a.db.execute(
        "SELECT payload FROM decisions WHERE key=?", (key,)
    ).fetchone()[0]
    revised = frame(NOW + timedelta(seconds=10))
    revised["bars"][3].update(high=10000.0, close=10000.0)
    e.observe(revised, [])
    assert (
        a.db.execute("SELECT payload FROM decisions WHERE key=?", (key,)).fetchone()[0]
        == before
    )
    a.close()


def test_frozen_run_rejects_reconfiguration(tmp_path):
    _, a = engine(tmp_path)
    a.close()
    changed = deepcopy(CONFIG)
    changed["min_efficiency"] = 0.4
    with pytest.raises(ValueError, match="Frozen run"):
        Archive(tmp_path / "edge.sqlite3", changed, NOW.isoformat())


def test_refuses_foreign_database(tmp_path):
    path = tmp_path / "edge.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE holdings(symbol TEXT)")
    with pytest.raises(ValueError, match="another application"):
        Archive(path, CONFIG, NOW.isoformat())
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("holdings",)]


def test_empty_results_are_unknown_and_not_promoted(tmp_path):
    _, a = engine(tmp_path)
    a.close()
    report = read_report(tmp_path / "edge.sqlite3")
    assert report["integrity"] == "ok"
    for b in report["books"].values():
        assert b["win_rate"] is None and b["net_expectancy"] is None
        assert b["evidence_status"] == "insufficient_sample"
        assert b["promotion"] == "not_authorized"


def test_many_correlated_trades_are_not_many_independent_sessions():
    trades = [{"net_pnl": 10, "stressed_net_pnl": 5, "session": "2026-10-21"}] * 250
    result = summarize(trades, 1, CONFIG)
    assert result["evidence_status"] == "insufficient_sample"
    assert result["expectancy_session_bootstrap_95pct"] is None


def test_missing_minutes_and_partial_bucket_not_fabricated():
    start = NOW.replace(hour=4, minute=0, second=0)
    rows = [
        {
            "time": (start + timedelta(minutes=i)).isoformat(),
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
        }
        for i in range(9)
    ]
    bars = aggregate_five(rows, (start + timedelta(minutes=9)).isoformat())
    assert len(bars) == 1
    assert (
        len(
            aggregate_five(
                rows[:2] + rows[3:], (start + timedelta(minutes=9)).isoformat()
            )
        )
        == 0
    )


def test_quote_receipt_does_not_replace_missing_exchange_timestamp():
    q = normalize_quote(
        {
            "instrument_token": "K",
            "last_trade_time": 1792558800000,
            "depth": {
                "buy": [{"price": 100, "quantity": 75}],
                "sell": [{"price": 101, "quantity": 75}],
            },
        },
        NOW.isoformat(),
    )
    assert q["time"] is None and q["last_trade_time"] is not None
    assert q["bid"] == 100


def test_expiry_day_blocks_entry_but_allows_held_exit():
    c = {**frame()["contracts"]["CE"], "expiry": "2026-10-21"}
    assert quote_problem(quote(), c, NOW, CONFIG) == "expiry_day_excluded"
    assert quote_problem(quote(), c, NOW, CONFIG, entry=False) is None


def test_research_reader_rejects_order_endpoints_before_network_access():
    with pytest.raises(ValueError, match="allowlist"):
        asyncio.run(QuoteReader("not-a-real-token").get("/order/place", {}))


def test_time_exit_still_closes_with_fresh_quote_during_candle_gap(tmp_path):
    e, a = engine(tmp_path)
    e.observe(frame(), [])
    entered = NOW + timedelta(seconds=15)
    e.observe(frame(entered), [quote(entered)])
    later = NOW + timedelta(minutes=61)
    e.observe(frame(later), [{**quote(later), "bid": 120.0, "ask": 121.0}])
    trades = [json.loads(r[0]) for r in a.db.execute("SELECT payload FROM trades")]
    assert len(trades) == 2 and all(t["exit_reason"] == "time_stop" for t in trades)
    a.close()


def test_default_report_withholds_holdout_pnl(tmp_path):
    cfg = deepcopy(CONFIG)
    cfg["evaluation"] = {
        "development_sessions": 2,
        "holdout_sessions": 1,
        "diagnostic_block_sessions": 1,
    }
    a = Archive(tmp_path / "edge.sqlite3", cfg, NOW.isoformat())
    with a.db:
        for i, pnl in enumerate([10.0, 10.0, 1000.0]):
            day = f"2020-01-0{i+1}"
            decision = {
                "key": "directional:challenger:NIFTY",
                "available_at": day + "T05:00:00+00:00",
                "bar_end": day + "T05:00:00+00:00",
                "reason": "candidate",
                "features": {"atr": 1},
            }
            a.db.execute(
                "INSERT INTO decisions VALUES(?,?)", (day, json.dumps(decision))
            )
            trade = {
                "lane": "directional",
                "variant": "challenger",
                "symbol": "NIFTY",
                "session": day,
                "exit_at": day + "T06:00:00+00:00",
                "net_pnl": pnl,
                "stressed_net_pnl": pnl - 1,
            }
            a.db.execute("INSERT INTO trades(payload) VALUES(?)", (json.dumps(trade),))
    a.close()
    sealed = read_report(tmp_path / "edge.sqlite3")["books"]["directional:challenger"]
    assert sealed["net_pnl"] == 20.0 and sealed["holdout"]["status"] == "sealed"
    assert "net_pnl" not in sealed["holdout"]
    revealed = read_report(tmp_path / "edge.sqlite3", unseal_holdout=True)["books"][
        "directional:challenger"
    ]
    assert revealed["holdout"]["net_pnl"] == 1000.0


def test_mcx_depth_contracts_are_distinct_from_price_pnl_multiplier():
    contract = {
        **frame()["contracts"]["CE"],
        "instrument_type": "FUT",
        "lot_size": 1,
        "pnl_units_per_lot": 100,
        "depth_required": 1,
        "depth_quantity_basis": "MCX_contract_count",
    }
    q = {**quote(), "bid": 146631.0, "ask": 146670.0, "bid_size": 2, "ask_size": 2}
    assert quote_problem(q, contract, NOW, CONFIG) is None
    assert (
        quote_problem(q, {**contract, "pnl_units_per_lot": 0}, NOW, CONFIG)
        == "insufficient_displayed_size"
    )


def test_failed_archive_transaction_does_not_hide_retried_inputs(tmp_path, monkeypatch):
    e, a = engine(tmp_path)
    original = a.event

    def broken(kind, when, payload):
        original(kind, when, payload)
        if kind == "quote":
            raise RuntimeError("simulated persistence error")

    monkeypatch.setattr(a, "event", broken)
    with pytest.raises(RuntimeError):
        e.observe(frame(), [quote()])
    monkeypatch.setattr(a, "event", original)
    e.observe(frame(), [quote()])
    assert (
        a.db.execute("SELECT COUNT(*) FROM events WHERE kind='frame'").fetchone()[0]
        == 1
    )
    a.close()
