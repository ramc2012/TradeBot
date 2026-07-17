"""Tests for the Auction-Intelligence COMMODITY sleeve (MCX evening session).

Covers: commodity universe resolution, market profile built from the unified
1-minute commodity store, tick-first order flow off the MCX tape, the futures
result-row translation + direction-aware paper booking, evening-window gating,
and paper-book separation from the NSE index auction book.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import auction_intelligence.commodity as commodity
import auction_intelligence.live as live
from auction_intelligence.schemas import (
    AgentDecision,
    ExecutionInstruction,
    RegimeAssessment,
    RiskDecision,
)
from institutional_convergence.paper import ConvergencePaperBook


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# ── Fixtures / helpers ───────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def _gold_session_rows(day: str, *, base: float = 73000.0, minutes: int = 210) -> list[dict]:
    """Synthetic GOLD 1-minute rows for one MCX session (09:00 onward, IST)."""
    start = datetime.fromisoformat(f"{day}T09:00:00+05:30")
    rows: list[dict] = []
    price = base
    for i in range(minutes):
        ts = start + timedelta(minutes=i)
        price = base + (i % 40) * 2.0  # mild oscillation inside a coarse band
        rows.append(
            {
                "time": ts.isoformat(),
                "open": price,
                "high": price + 6.0,
                "low": price - 6.0,
                "close": price + 1.0,
                "volume": 120.0,
            }
        )
    return rows


def _synthetic_ticks(n: int = 8, *, ltp: float = 73010.0) -> list[dict]:
    """Rows shaped like auction_intelligence.live._fetch_recent_tick_rows output."""
    now = datetime.now(UTC)
    ticks: list[dict] = []
    for i in range(n):
        ticks.append(
            {
                "timestamp": now - timedelta(seconds=(n - i) * 5),
                "ltp": ltp + i,
                "bid": ltp + i - 1.0,
                "ask": ltp + i + 1.0,
                "bid_qty": 40.0,
                "ask_qty": 35.0,
                "total_buy_qty": 500.0,
                "total_sell_qty": 480.0,
                "volume": 1000.0 + i * 10,
                "oi": 20000.0,
            }
        )
    return ticks


def _make_bundle(*, action: str, spot: float, stop: float, target: float, confidence: float = 0.72):
    """A minimal analysis-bundle stand-in carrying just what
    bundle_to_result_row reads (decisions / execution plan / risk / regime)."""
    decision = AgentDecision(
        agent_name="swing",
        action=action,
        confidence=confidence,
        entry_price=spot,
        stop_price=stop,
        target_price=target,
        quantity=10,
        sleeve_fraction=0.04,
        rationale=["synthetic"],
        metadata={},
    )
    execution = ExecutionInstruction(
        agent_name="swing",
        symbol="GOLD FUT",
        action=action,
        style="AGGRESSIVE",
        order_type="MARKET",
        limit_price=None,
        slices=1,
        cancel_after_seconds=5,
        rationale=["synthetic"],
        quantity=10,
        broker_action="BUY" if action == "LONG" else "SELL",
        underlying_symbol="GOLD",
        instrument_type="FUT",
    )
    regime = RegimeAssessment(label="trend_continuation", confidence=0.6, allowed_directions=[action], reasons=[])
    risk = RiskDecision(allowed=True, kill_switch=False, max_size_multiplier=1.0, reasons=["ok"])
    return SimpleNamespace(
        agent_decisions=[decision],
        execution_plan=[execution],
        risk=risk,
        regime=regime,
    )


# ── Universe resolution ──────────────────────────────────────────────────────


def test_configured_roots_are_canonical_and_spec_backed() -> None:
    roots = commodity.configured_commodity_auction_roots()
    assert roots == ["GOLD", "SILVERM", "CRUDEOIL"]


def test_resolve_universe_maps_roots_to_active_contracts(monkeypatch) -> None:
    async def _fake_resolve(root, *, session_date):
        return {"symbol": f"MCX:{root}26AUGFUT", "root": root, "lot_size": 10, "expiry": "2026-08-31"}

    monkeypatch.setattr(commodity, "resolve_active_upstox_mcx_future", _fake_resolve)
    universe = _run(commodity.resolve_commodity_auction_universe(datetime(2026, 7, 16, 21, 0, tzinfo=IST)))
    assert universe["resolved_count"] == 3
    assert universe["unresolved"] == []
    assert universe["contracts"]["GOLD"]["symbol"] == "MCX:GOLD26AUGFUT"


def test_resolve_universe_reports_unresolved_roots(monkeypatch) -> None:
    async def _fake_resolve(root, *, session_date):
        return {"symbol": f"MCX:{root}26AUGFUT"} if root == "GOLD" else None

    monkeypatch.setattr(commodity, "resolve_active_upstox_mcx_future", _fake_resolve)
    universe = _run(commodity.resolve_commodity_auction_universe(datetime(2026, 7, 16, 21, 0, tzinfo=IST)))
    assert set(universe["contracts"]) == {"GOLD"}
    assert set(universe["unresolved"]) == {"SILVERM", "CRUDEOIL"}


# ── Market profile from the commodity store ──────────────────────────────────


def test_analysis_builds_market_profile_from_commodity_bars(monkeypatch) -> None:
    rows = _gold_session_rows("2026-07-13") + _gold_session_rows("2026-07-14")

    async def _fake_rows(root, contract_symbol, *, lookback_days=7):
        assert root == "GOLD"
        return rows

    monkeypatch.setattr(commodity, "_load_commodity_minute_rows", _fake_rows)
    # Keep order-flow off the DB regardless of live/replay classification.
    async def _no_ticks(symbol, *, snapshot_end, symbol_code=None):
        return []

    monkeypatch.setattr(live, "_fetch_recent_tick_rows", _no_ticks)

    contract = {"symbol": "MCX:GOLD26AUGFUT", "root": "GOLD", "lot_size": 10}
    analysis = _run(commodity.build_commodity_analysis("GOLD", contract=contract))
    bundle = analysis["bundle"]

    # Profile is built from the commodity bars at the coarse GOLD value tick.
    assert bundle.market_profile.symbol == "GOLD FUT"
    assert bundle.market_profile.tick_size == 20.0
    assert 72900.0 <= bundle.market_profile.poc <= 73200.0
    assert bundle.market_profile.val <= bundle.market_profile.poc <= bundle.market_profile.vah
    # 30-minute auction bars were built (>=4 required).
    assert len(analysis["request"]["bars"]) >= 4
    assert analysis["request"]["metadata"]["market"] == "MCX"


# ── Tick-first order flow off the MCX tape ───────────────────────────────────


def test_order_flow_is_tick_first_from_mcx_tape(monkeypatch) -> None:
    async def _fake_ticks(symbol, *, snapshot_end, symbol_code=None):
        assert symbol == "MCX:GOLD26AUGFUT"  # reads the RESOLVED contract's tape
        return _synthetic_ticks(8)

    monkeypatch.setattr(live, "_fetch_recent_tick_rows", _fake_ticks)

    current_rows = _gold_session_rows("2026-07-14", minutes=130)
    result = _run(
        live._build_order_flow_inputs(
            app_symbol="MCX:GOLD26AUGFUT",
            current_rows=current_rows,
            current_tick=None,
            quote_override=None,
            tick_size=20.0,
            snapshot_mode="live_session",
            symbol_code="GOLD",
        )
    )
    assert result["order_flow_source"] == "tick_reconstruction"
    assert result["quote_source"] == "market_ticks"
    assert len(result["quote_history"]) >= 4


# ── Futures result-row translation ───────────────────────────────────────────


def test_bundle_to_result_row_maps_long_signal_to_futures_order() -> None:
    bundle = _make_bundle(action="LONG", spot=73000.0, stop=72500.0, target=74000.0)
    row = commodity.bundle_to_result_row(
        "GOLD",
        bundle=bundle,
        contract_symbol="MCX:GOLD26AUGFUT",
        spot=73000.0,
        snapshot_time=datetime(2026, 7, 16, 21, 0, tzinfo=IST),
        lot_size=10,
    )
    assert row["status"] == "actionable_paper"
    assert row["action"] == "LONG"
    assert row["futures_contract"] == "MCX:GOLD26AUGFUT"
    assert row["risk"]["entry"] == 73000.0
    assert row["risk"]["stop"] == 72500.0
    assert row["risk"]["lot_size"] == 10
    assert row["long_setup"] and row["long_setup"]["bar_time"]


def test_bundle_to_result_row_rejects_wrong_side_levels() -> None:
    # LONG with stop ABOVE entry is degenerate → not actionable.
    bundle = _make_bundle(action="LONG", spot=73000.0, stop=73500.0, target=74000.0)
    row = commodity.bundle_to_result_row(
        "GOLD",
        bundle=bundle,
        contract_symbol="MCX:GOLD26AUGFUT",
        spot=73000.0,
        snapshot_time=datetime(2026, 7, 16, 21, 0, tzinfo=IST),
        lot_size=10,
    )
    assert row["status"] != "actionable_paper"
    assert row["action"] == "FLAT"


# ── Cycle: booking + evening gating + book separation ────────────────────────


def test_cycle_opens_direction_aware_futures_position(monkeypatch, tmp_path) -> None:
    book = ConvergencePaperBook(tmp_path / "commodity_paper.json", squareoff=commodity._SQUAREOFF, entry_quarantine=None)
    monkeypatch.setattr(commodity, "commodity_auction_paper_book", book)
    monkeypatch.setattr(commodity, "_mcx_open", lambda now: True)
    monkeypatch.setattr(commodity, "_now_ist", lambda: datetime(2026, 7, 16, 21, 0, tzinfo=IST))

    async def _fake_universe(now=None):
        return {
            "roots": ["GOLD"],
            "contracts": {"GOLD": {"symbol": "MCX:GOLD26AUGFUT", "root": "GOLD", "lot_size": 10}},
            "resolved_count": 1,
            "unresolved": [],
            "session_date": "2026-07-17",
        }

    async def _fake_subs(symbols):
        return len(symbols)

    async def _fake_analysis(root, *, contract=None, now=None):
        return {
            "bundle": _make_bundle(action="LONG", spot=73000.0, stop=72500.0, target=74000.0),
            "contract_symbol": "MCX:GOLD26AUGFUT",
            "spot": 73000.0,
            "snapshot_time": datetime(2026, 7, 16, 21, 0, tzinfo=IST),
            "lot_size": 10,
            "order_flow_source": "tick_reconstruction",
        }

    monkeypatch.setattr(commodity, "resolve_commodity_auction_universe", _fake_universe)
    monkeypatch.setattr(commodity.market_data_router, "add_subscriptions", _fake_subs)
    monkeypatch.setattr(commodity, "build_commodity_analysis", _fake_analysis)

    payload = _run(commodity.run_commodity_market_hours_cycle())
    assert payload["status"] == "ok"
    assert payload["actionable_count"] == 1
    assert payload["market"] == "MCX"

    summary = book.summary()
    assert summary["open_count"] == 1
    position = summary["open_positions"][0]
    assert position["symbol"] == "GOLD"
    assert position["direction"] == "LONG"
    assert position["entry_price"] == 73000.0

    # A subsequent cycle marking a HIGHER price must show a profitable exit for
    # the LONG (direction-aware futures PnL), proving futures semantics.
    def _mark_at(price):
        return [
            {
                "kind": "commodity",
                "symbol": "GOLD",
                "market": "MCX",
                "status": "flat",
                "action": "FLAT",
                "spot": price,
                "futures_contract": "MCX:GOLD26AUGFUT",
            }
        ]

    book.sync(_mark_at(74000.0), datetime(2026, 7, 16, 21, 5, tzinfo=IST))
    after = book.summary()
    # target1 partial booked a gain on the still-open runner (LONG profits as
    # the future rises — direction-aware futures PnL).
    assert after["open_positions"][0]["realized_pnl"] > 0
    # Squaring off the remainder at the higher price realizes a net gain.
    book.sync(_mark_at(74000.0), datetime(2026, 7, 16, 23, 20, tzinfo=IST))
    assert book.summary()["realized_pnl"] > 0


def test_cycle_gated_closed_outside_mcx_hours(monkeypatch) -> None:
    monkeypatch.setattr(commodity, "_mcx_open", lambda now: False)
    monkeypatch.setattr(commodity, "_now_ist", lambda: datetime(2026, 7, 16, 3, 0, tzinfo=IST))
    payload = _run(commodity.run_commodity_market_hours_cycle())
    assert payload["status"] == "market_closed"
    assert payload["market"] == "MCX"
    assert "next_run_at" in payload


def test_commodity_book_is_separate_from_nse_index_book() -> None:
    from api.routers.auction_intelligence import _paper_book as nse_book

    commodity_path = str(commodity.commodity_auction_paper_book.path)
    assert "auction_intelligence_commodity" in commodity_path
    assert str(nse_book.path) != commodity_path
    # The NSE book path must not live under the commodity directory.
    assert "auction_intelligence_commodity" not in str(nse_book.path)
