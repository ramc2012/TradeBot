"""Distribution math and paper integrity: independent invariants, not snapshots."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from directional_options.distribution import surface_curves
from directional_options.index_paper.costs import CostModel, estimate_fill, option_fee_schedule
from directional_options.selector import calendar_days_to_expiry
from directional_options.paper import DirectionalOptionsPaperStore
from market_data.option_chain import OptionChainService


def _flat_slice(lo=-.5, hi=.5):
    T, iv = 30/365, .2
    return dict(svi_a=iv*iv*T, svi_b=0, svi_rho=0, svi_m=0, svi_s=.1,
                forward=25000, tenor_years=T, k_min=lo, k_max=hi,
                fit_status="ok", butterfly_ok=True, calendar_ok=True)


def test_density_matches_lognormal_and_retains_missing_tail_mass():
    row = _flat_slice(-.03, .03)
    result = surface_curves(row)
    assert result["status"] == "ok"
    T, sigma, F = 30/365, .2, 25000
    for point in result["smile"][::20]:
        K = point["strike"]
        d2 = (np.log(F/K)-.5*sigma*sigma*T)/(sigma*np.sqrt(T))
        assert point["density"] == pytest.approx(norm.pdf(d2)/(K*sigma*np.sqrt(T)), rel=1e-9)
    assert 0 < result["density_mass"] < .5
    assert min(p["moneyness_pct"] for p in result["smile"]) >= (np.exp(-.03)-1)*100 - 1e-10


@pytest.mark.parametrize("bad", [{"butterfly_ok": False}, {"calendar_ok": False}, {"fit_status": "no_convergence"}, {"svi_a": float("nan")}, {"tenor_years": 0}])
def test_refused_fits_never_generate_density_or_stress(bad):
    result = surface_curves({**_flat_slice(), **bad})
    assert result["status"] == "unavailable"
    assert result["smile"] == result["scenarios"] == []


def test_shocks_respect_put_call_parity_and_decay():
    r = surface_curves(_flat_slice())
    for cell in r["scenarios"]:
        assert cell["CE"]-cell["PE"] == pytest.approx(25000*cell["move_pct"]/100, abs=.001)
    zero = next(c for c in r["scenarios"] if c["move_pct"] == c["vol_points"] == 0)
    assert zero["CE"] < 0 and zero["PE"] < 0


def test_exchange_close_clock_refuses_expired_contracts():
    expiry = date(2026, 9, 8)
    assert calendar_days_to_expiry(expiry, pd.Timestamp("2026-09-08T09:59:00Z")) == pytest.approx(1/1440)
    assert calendar_days_to_expiry(expiry, pd.Timestamp("2026-09-08 15:30")) == 0
    assert calendar_days_to_expiry(expiry, pd.Timestamp("2026-09-09 09:15")) < 0


def test_dated_stt_and_exchange_fees():
    old = option_fee_schedule(date(2026, 3, 31), "NIFTY").charges(100, 100, "sell")
    new = option_fee_schedule(date(2026, 4, 1), "NIFTY").charges(100, 100, "sell")
    assert old["stt"] == 10
    assert new["stt"] == 15
    assert option_fee_schedule(date(2026, 9, 8), "SENSEX").charges(100, 100, "buy")["exchange"] == pytest.approx(3.25)


def test_adverse_fills_are_tick_aligned_and_cannot_monetise_zero():
    for premium in (0, .02, .05, 100.03):
        buy = estimate_fill(premium, 100, "buy", "entry")
        sell = estimate_fill(premium, 100, "sell", "exit")
        assert buy.fill_price >= premium >= sell.fill_price >= 0
        assert buy.fill_price/.05 == pytest.approx(round(buy.fill_price/.05))
        assert sell.fill_price/.05 == pytest.approx(round(sell.fill_price/.05))
    assert estimate_fill(0, 100, "sell", "exit").fill_price == 0


class SharedRedis:
    def __init__(self):
        self.values = {}
    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True
    async def get(self, key):
        return self.values.get(key)
    async def eval(self, _script, _count, key, token):
        if self.values.get(key) == token:
            self.values.pop(key)
            return 1
        return 0


@pytest.mark.asyncio
async def test_two_chain_workers_coalesce_broker_and_compute(monkeypatch):
    redis = SharedRedis()
    async def get_redis():
        return redis
    monkeypatch.setenv("SHARED_MP_REDIS_URL", "redis://shared")
    monkeypatch.setattr("market_data.option_chain.get_redis", get_redis)
    calls = []
    async def refresh(symbol, expiry):
        calls.append((symbol, expiry))
        await asyncio.sleep(.02)
        await redis.set(f"oc:{symbol}:{expiry}", '{"timestamp":"'+datetime.now(timezone.utc).isoformat()+'"}')
    a, b = OptionChainService(), OptionChainService()
    monkeypatch.setattr(a, "_refresh_once", refresh)
    monkeypatch.setattr(b, "_refresh_once", refresh)
    await asyncio.gather(a._refresh("NIFTY", "2026-09-29"), b._refresh("NIFTY", "2026-09-29"))
    await b._refresh("NIFTY", "2026-09-29")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_chain_quote_uses_observation_not_cache_write(monkeypatch):
    from directional_options.chain_analytics import chain_strike_quote, option_chain_service
    now = datetime.now(timezone.utc)
    payload = {"entries": [{"strike": 25000, "option_type": "CE", "ltp": 100}],
               "timestamp": now.isoformat(), "data_quality": {"execution_ready": True, "observed_at": (now-timedelta(days=2)).isoformat()}}
    async def cached(*args):
        return payload
    monkeypatch.setattr(option_chain_service, "get_cached", cached)
    assert await chain_strike_quote("NIFTY", "2026-09-29", 25000, "CE") is None
    payload["data_quality"]["observed_at"] = now.isoformat()
    quote = await chain_strike_quote("NIFTY", "2026-09-29", 25000, "CE")
    assert quote["premium"] == 100 and quote["mark_time"] == now.isoformat()


def _memory_book(stores, monkeypatch, positions=None, cash=3_000_000):
    state = {"open_positions": deepcopy(positions or []), "closed_positions": []}
    journal = []
    async def load():
        await asyncio.sleep(.005)
        return deepcopy(state)
    async def save(payload):
        state.update(deepcopy(payload))
    async def summary(opens, closes):
        return {"available_capital": cash - sum(p["entry_premium"]*p["quantity_units"] for p in opens), "open_positions": len(opens)}
    async def windows():
        return 0, 0
    async def append(row):
        journal.append(row)
    async def noop(**kwargs):
        pass
    monkeypatch.setattr("directional_options.paper.paper_trade_recorder.record_event", noop)
    for store in stores:
        for name, fn in (("_load_positions", load), ("_save_positions", save), ("_summary", summary), ("realized_pnl_windows", windows), ("_append_journal", append)):
            monkeypatch.setattr(store, name, fn)
    return state, journal


def _proposal(symbol="NIFTY"):
    return {"selection": {"underlying": symbol, "timeframe": "3minute"}, "snapshot": {
        "as_of": datetime.now(timezone.utc).isoformat(), "underlying": symbol, "spot_price": 25000,
        "data_status": {"execution_ready": True}, "signal": {"direction": "CE"},
        "selected_contract": {"instrument_key": symbol+"|CE", "option_price": 100, "option_type": "CE", "strike": 25000, "expiry": "2026-12-29", "lot_size": 100},
        "risk": {"approved": True, "quantity_units": 100, "quantity_lots": 1}}}


@pytest.mark.asyncio
async def test_cross_instance_funding_prevents_two_workers_spending_same_cash(tmp_path, monkeypatch):
    a, b = DirectionalOptionsPaperStore(tmp_path), DirectionalOptionsPaperStore(tmp_path)
    state, journal = _memory_book([a,b], monkeypatch, cash=15000)
    await asyncio.gather(a.sync_snapshot(_proposal()), b.sync_snapshot(_proposal("BANKNIFTY")))
    assert len(state["open_positions"]) == 1
    assert any(r.get("status") == "funding_skip" for r in journal)
    assert state["open_positions"][0]["entry_premium"] > 100


@pytest.mark.asyncio
async def test_whole_book_stop_does_not_depend_on_signal_scan(tmp_path, monkeypatch):
    held = {"position_id": "held", "underlying": "ITC", "entry_premium": 100, "latest_premium": 100,
            "quantity_units": 100, "status": "open", "mark_time": (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}
    store = DirectionalOptionsPaperStore(tmp_path)
    state, _ = _memory_book([store], monkeypatch, [held])
    async def quote(row):
        return {"premium": 50, "mark_time": datetime.now(timezone.utc).isoformat(), "price_source": "test"}
    result = await store.refresh_held_marks(quote)
    assert result["marked"] == result["closed"] == 1
    assert state["open_positions"] == []
    assert state["closed_positions"][0]["realized_pnl"] < -5000


@pytest.mark.asyncio
async def test_zero_exit_and_costs_are_preserved(tmp_path, monkeypatch):
    store = DirectionalOptionsPaperStore(tmp_path)
    state, _ = _memory_book([store], monkeypatch)
    await store.sync_snapshot(_proposal())
    row = state["open_positions"][0]
    store._close_position(row, mark={"premium": 0, "mark_time": datetime.now(timezone.utc).isoformat()}, close_time=datetime.now(timezone.utc).isoformat(), close_reason="test_zero")
    assert row["exit_premium"] == 0
    assert row["realized_pnl"] == pytest.approx(-row["entry_premium"]*100-row["transaction_cost"], abs=.01)


def test_surface_fit_shared_across_consumers_without_mutation_leak(monkeypatch):
    import directional_options.vol.surface as surface
    ts = datetime(2049, 1, 7, tzinfo=timezone.utc)
    expiry = date(2049, 1, 28)
    calls = []
    def compute(symbol, stamp, exp, rows, **kwargs):
        calls.append(rows)
        return surface.SurfaceSlice(symbol, stamp, exp, .05, 25000, 25000, 25000, reason="original")
    monkeypatch.setattr(surface, "_build_slice_from_rows_uncached", compute)
    a = surface.build_slice_from_rows("NIFTY", ts, expiry, [{"close": 100}])
    a.reason = "consumer quarantine"
    b = surface.build_slice_from_rows("NIFTY", ts, expiry, [{"close": 100}])
    assert len(calls) == 1 and b.reason == "original"
    surface.build_slice_from_rows("NIFTY", ts, expiry, [{"close": 101}])
    assert len(calls) == 2


@pytest.mark.parametrize("age", [None, -60, 121, 86400])
def test_paper_store_refuses_stale_untimed_or_future_exit(tmp_path, age):
    now = datetime.now(timezone.utc)
    mark = {"premium": 12}
    if age is not None:
        mark["mark_time"] = (now-timedelta(seconds=age)).isoformat()
    row = {"entry_premium": 100, "quantity_units": 100, "status": "open"}
    with pytest.raises(ValueError):
        DirectionalOptionsPaperStore(tmp_path)._close_position(row, mark=mark, close_time=now.isoformat(), close_reason="test")
    assert row["status"] == "open"
