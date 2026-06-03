"""Execution-layer tests for the Gann paper agent (gann_tp_delta.agent).

Exercise the pure decision/sizing/exit helpers directly (no async data layer):
equal-notional sizing, dual-instrument build, break-even + trailing on the
underlying, Gann stop/target exits, short-futures P&L sign, and the headline
whipsaw fix — opposite-signal exits ONLY on a high-conviction reversal.
"""
from __future__ import annotations

from gann_tp_delta.agent import GannTPDeltaPaperAgent
from gann_tp_delta.config import clone_default_config

CFG = clone_default_config()
RISK = CFG["risk"]


def _agent() -> GannTPDeltaPaperAgent:
    return GannTPDeltaPaperAgent(service=object(), config=CFG)


# ─── Sizing & dual-instrument build ──────────────────────────────────────────


def test_futures_equal_notional_sizing():
    ag = _agent()
    decision = {
        "instrument_type": "FUTURES", "underlying": "CRUDEOIL", "spot_price": 5000.0,
        "stop_underlying": 4950.0, "risk_per_unit": 50.0, "targets_underlying": [5100.0, 5200.0],
        "thesis_side": "long", "direction": "long", "archetype": "continuation",
        "conviction": 5.0, "size_factor": 1.0,
        "futures": {"underlying": "CRUDEOIL", "lot_size": 100, "price": 5000.0,
                    "trading_symbol": "CRUDEOIL FUT", "tick_size": 1.0},
    }
    pos = ag._build_position(decision, {})
    assert pos is not None
    assert pos["instrument_type"] == "FUTURES"
    # ₹1.5M / (100 × 5000) = 3 lots
    assert pos["qty_lots"] == 3 and pos["qty_units"] == 300
    assert pos["stop_underlying"] == 4950.0
    assert pos["target_price"] == 5100.0
    assert pos["direction"] == "long"


def test_reversal_size_factor_shrinks_lots():
    ag = _agent()
    decision = {
        "instrument_type": "FUTURES", "underlying": "CRUDEOIL", "spot_price": 5000.0,
        "stop_underlying": 5050.0, "risk_per_unit": 50.0, "targets_underlying": [4900.0],
        "thesis_side": "short", "direction": "short", "archetype": "reversal",
        "conviction": 7.0, "size_factor": 0.5,
        "futures": {"underlying": "CRUDEOIL", "lot_size": 100, "price": 5000.0,
                    "trading_symbol": "CRUDEOIL FUT", "tick_size": 1.0},
    }
    pos = ag._build_position(decision, {})
    assert pos["qty_lots"] < 3 and pos["qty_lots"] >= 1  # half-sized vs the 3-lot full size


def test_option_premium_budget_sizing_and_hard_stop():
    ag = _agent()
    decision = {
        "instrument_type": "OPTION", "underlying": "NIFTY", "spot_price": 23000.0,
        "stop_underlying": 22900.0, "risk_per_unit": 100.0, "targets_underlying": [23200.0],
        "thesis_side": "long", "direction": "long_call", "option_type": "CE",
        "archetype": "continuation", "conviction": 5.0, "size_factor": 1.0,
        "option": {"ltp": 200.0, "lot_size": 75, "expiry": "2026-06-25", "strike": 23000,
                   "instrument_key": "K", "trading_symbol": "NIFTY25JUN23000CE"},
    }
    pos = ag._build_position(decision, {})
    assert pos["instrument_type"] == "OPTION"
    # ₹50k / (75 × 200) ≈ 3.33 → 3 lots
    assert pos["qty_lots"] == 3 and pos["qty_units"] == 225
    # premium hard stop = entry × (1 - 55%)
    assert abs(pos["premium_hard_stop"] - 90.0) < 1e-6
    assert pos["thesis_side"] == "long"


# ─── Break-even & trailing on the underlying ─────────────────────────────────


def _long_fut(**over):
    base = {
        "thesis_side": "long", "instrument_type": "FUTURES", "direction": "long",
        "entry_underlying": 5000.0, "risk_per_unit": 50.0, "stop_underlying": 4950.0,
        "peak_underlying": 5000.0, "trough_underlying": 5000.0, "bars_held": 0,
        "targets_underlying": [5300.0],
    }
    base.update(over)
    return base


def test_breakeven_moves_stop_to_entry_after_1r():
    ag = _agent()
    pos = _long_fut()
    ag._update_underlying_tracking(pos, 5050.0, RISK)  # +1R
    assert pos["be_done"] is True
    assert pos["stop_underlying"] == 5000.0  # lifted to entry


def test_trailing_locks_profit_after_trail_start():
    ag = _agent()
    pos = _long_fut()
    ag._update_underlying_tracking(pos, 5100.0, RISK)  # +2R, peak_r=2 ≥ 1.5
    assert pos["trail_active"] is True
    assert pos["stop_underlying"] == 5050.0  # entry + (2-1)·R


# ─── Gann stop / target exits ────────────────────────────────────────────────


def test_gann_stop_exit_long():
    ag = _agent()
    pos = _long_fut(stop_underlying=4950.0)
    assert ag._risk_exit_reason(pos, 4949.0, {}, risk_cfg=RISK, rev_min=6.5) == "gann_stop"


def test_gann_target_exit_long():
    ag = _agent()
    pos = _long_fut(targets_underlying=[5100.0])
    assert ag._risk_exit_reason(pos, 5100.0, {}, risk_cfg=RISK, rev_min=6.5) == "gann_target"


def test_no_exit_while_in_band():
    ag = _agent()
    pos = _long_fut(stop_underlying=4950.0, targets_underlying=[5200.0])
    assert ag._risk_exit_reason(pos, 5010.0, {}, risk_cfg=RISK, rev_min=6.5) is None


# ─── The whipsaw fix: opposite exit only on HIGH conviction ───────────────────


def test_weak_opposite_signal_does_not_close():
    ag = _agent()
    pos = _long_fut(stop_underlying=4900.0, targets_underlying=[5300.0])
    weak = {"side": "short", "conviction": 4.0, "archetype": "reversal"}
    assert ag._risk_exit_reason(pos, 5010.0, weak, risk_cfg=RISK, rev_min=6.5) is None


def test_strong_opposite_signal_closes():
    ag = _agent()
    pos = _long_fut(stop_underlying=4900.0, targets_underlying=[5300.0])
    strong = {"side": "short", "conviction": 7.0, "archetype": "reversal"}
    assert ag._risk_exit_reason(pos, 5010.0, strong, risk_cfg=RISK, rev_min=6.5) == "opposite_high_conviction"


# ─── Short-futures P&L sign ──────────────────────────────────────────────────


def test_short_futures_pnl_sign():
    ag = _agent()
    pos = {"instrument_type": "FUTURES", "direction": "short", "entry_price": 5000.0,
           "current_price": 4900.0, "qty_units": 100}
    closed = ag._close_position(pos, "gann_target")
    assert closed["realized_pnl"] == 10000.0  # short gained as price fell


def test_time_stop_when_stalled():
    ag = _agent()
    pos = _long_fut(bars_held=999, stop_underlying=4900.0, targets_underlying=[5300.0])
    # flat (≈0R) and well past the time-stop window → drop it
    assert ag._risk_exit_reason(pos, 5001.0, {}, risk_cfg=RISK, rev_min=6.5) == "time_stop"


# ─── run_once end-to-end smoke (catches scoping/flow bugs the unit tests miss) ─


def test_run_once_opens_option_position(tmp_path, monkeypatch):
    """Drive a full run_once with a stubbed service + watchlist. Guards the
    run_once variable flow (e.g. the daily-loss-cap block must run AFTER the
    state load) and the index→option open path end to end."""
    import asyncio
    from types import SimpleNamespace
    from gann_tp_delta import agent as agent_mod
    from gann_tp_delta.config import clone_default_config

    cfg = clone_default_config()
    cfg["universe"] = ["NIFTY"]              # indices only → no commodity data layer
    cfg["paper"]["journal_root"] = tmp_path

    class _FakeService:
        def __init__(self):
            self.store = SimpleNamespace(directional_store=SimpleNamespace())

        async def live_snapshot(self, underlying, *a, **k):
            return {
                "status": "ready", "spot_price": 23000.0, "as_of": "2026-06-02T10:00:00+00:00",
                "underlying": underlying,
                "signal": {
                    "state": "bullish_setup", "side": "long", "archetype": "continuation",
                    "conviction": 6.0, "regime": "bull", "size_factor": 1.0,
                    "stop_underlying": 22900.0, "targets_underlying": [23300.0],
                    "risk_per_unit": 100.0, "bias": "bullish", "threshold": 4, "score": 6, "reasons": [],
                },
            }

    ag = agent_mod.GannTPDeltaPaperAgent(service=_FakeService(), config=cfg)

    async def fake_watchlist(*a, **k):
        return {"rows": [{
            "underlying": "NIFTY", "spot_price": 23000.0,
            "ce": {"ltp": 200.0, "lot_size": 75, "strike": 23000, "expiry": "2026-12-25",
                   "instrument_key": "K", "trading_symbol": "NIFTY25DEC23000CE"},
        }]}

    monkeypatch.setattr(agent_mod.atm_watchlist_service, "get_watchlist", fake_watchlist)

    out = asyncio.run(ag.run_once())
    assert out["last_run"]["scanned"] >= 1
    assert out["last_run"]["opened"] == 1
    assert out["summary"]["open_positions"] == 1
    assert out["last_run"].get("errors", 0) == 0


def test_commodity_conviction_floor_gates_weak_setups():
    """Commodities must clear the higher commodity_min_conviction bar; a setup
    that would open an index trade is gated for a commodity until it does."""
    ag = _agent()
    row = {"underlying": "CRUDEOIL", "is_commodity": True}

    def snap(conv):
        return {"spot_price": 5000.0, "as_of": "t", "underlying": "CRUDEOIL",
                "signal": {"state": "bullish_setup", "side": "long", "archetype": "continuation",
                           "conviction": conv, "regime": "bull", "size_factor": 1.0,
                           "stop_underlying": 4950.0, "targets_underlying": [5200.0],
                           "risk_per_unit": 50.0, "bias": "bullish"}}

    floor = CFG["strategy"]["commodity_min_conviction"]
    gated = ag._scan_decision(row, snap(floor - 1.0), min_score=0)
    assert gated["decision"] == "skip" and gated["reason"] == "conviction_floor"
    opened = ag._scan_decision(row, snap(floor + 0.5), min_score=0)
    assert opened["decision"] == "open" and opened["instrument_type"] == "FUTURES"


def test_per_underlying_conviction_floor_gates_banknifty():
    """BANKNIFTY (negative-EV at every floor in backtest) gets a per-underlying
    6.0 floor — a setup that clears the engine's 5.0 bar is still gated for it."""
    ag = _agent()
    bn_floor = CFG["strategy"]["per_underlying_min_conviction"]["BANKNIFTY"]
    row = {"underlying": "BANKNIFTY", "spot_price": 53000.0,
           "ce": {"ltp": 300.0, "lot_size": 35, "strike": 53000, "expiry": "2026-12-25",
                  "instrument_key": "K", "trading_symbol": "BN"}}

    def snap(conv):
        return {"spot_price": 53000.0, "as_of": "t", "underlying": "BANKNIFTY",
                "signal": {"state": "bullish_setup", "side": "long", "archetype": "continuation",
                           "conviction": conv, "regime": "bull", "size_factor": 1.0,
                           "stop_underlying": 52800.0, "targets_underlying": [53400.0],
                           "risk_per_unit": 200.0, "bias": "bullish"}}

    gated = ag._scan_decision(row, snap(bn_floor - 0.5), min_score=0)
    assert gated["decision"] == "skip" and gated["reason"] == "conviction_floor"
    opened = ag._scan_decision(row, snap(bn_floor + 0.2), min_score=0)
    assert opened["decision"] == "open" and opened["instrument_type"] == "OPTION"
