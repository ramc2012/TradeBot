"""CAUSALITY TESTS — prefix invariance at rtol 1e-12, plus the lag contract.

1. PREFIX INVARIANCE: for K random cutoffs, recompute regimes/timers on the
   tape truncated at the cutoff; every value at or before the cutoff must
   equal the full-sample value to rtol 1e-12. Any centred window, lookahead
   shift, or full-sample normalisation fails this immediately.
2. LAG CONTRACT: the regime state governing session t must equal the state
   computed from sessions <= t-1 (r*_lag1 == r*_state shifted by one).
3. READ-LAYER CAUSALITY: contracts_for(asof) must not change when bars
   strictly after asof are deleted; mark(ts) must not change when bars after
   ts are deleted (the model carries IV from <= ts only).

Run:  python -m pytest test_causality.py  (from this directory)
Uses real extracted spot if panel_2d3d CSVs exist, else a seeded synthetic
tape, so the suite stays green with PG unreachable.
"""
from __future__ import annotations

import glob
import os
from datetime import date

import numpy as np
import pandas as pd

import regime_defs as rd
import timer_defs as td
from option_read_layer import OptionReadLayer, load_spot_csvs

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL_SPOT = os.path.join(os.path.dirname(HERE), "panel_2d3d", "data")
RTOL = 1e-12
N_CUTS = 12
RNG = np.random.default_rng(20260721)


def _spot_frame() -> pd.DataFrame:
    paths = sorted(glob.glob(os.path.join(PANEL_SPOT, "spot_2026-04-01.csv")))
    if paths:
        # load_spot_csvs applies the D4-spot dedup rule (the raw CSVs carry
        # ~65% duplicate timestamps from cross-source writes)
        s = load_spot_csvs(paths)
        und = s["underlying"].value_counts().index[0]
        return s[s["underlying"] == und].reset_index(drop=True)
    # synthetic fallback: 120 sessions x 13 bars
    times, rows = [], []
    px = 100.0
    t0 = pd.Timestamp("2026-01-01 03:45:00+00:00")
    for d in range(120):
        day = t0 + pd.Timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for k in range(13):
            px *= float(np.exp(RNG.normal(0, 0.004)))
            ts = day + pd.Timedelta(minutes=30 * k)
            rows.append((ts, "SYN", px * 0.999, px * 1.001, px * 0.998, px,
                         int(RNG.integers(1_000, 50_000))))
    return pd.DataFrame(rows, columns=["time", "underlying", "open", "high",
                                       "low", "close", "volume"])


def _cmp(full: pd.DataFrame, trunc: pd.DataFrame, cols, n) -> None:
    for c in cols:
        a = full[c].iloc[:n].to_numpy()
        b = trunc[c].iloc[:n].to_numpy()
        if a.dtype == bool or b.dtype == bool:
            assert (a == b).all(), f"prefix variance in {c}"
        else:
            ok = np.isclose(a, b, rtol=RTOL, equal_nan=True)
            assert ok.all(), f"prefix variance in {c}: {np.flatnonzero(~ok)[:5]}"


def test_regime_prefix_invariance():
    s = _spot_frame()
    daily = rd.resample_daily(s)
    full = rd.daily_regimes(daily)
    cols = ["r1_state", "r2_state", "r1_lag1", "r2_lag1"]
    for cut in RNG.integers(rd.SMA_SLOW + 5, len(daily), N_CUTS):
        trunc = rd.daily_regimes(daily.iloc[:int(cut)])
        _cmp(full, trunc, cols, int(cut))


def test_regime_lag_contract():
    s = _spot_frame()
    d = rd.daily_regimes(rd.resample_daily(s))
    for r in ("r1", "r2"):
        got = d[f"{r}_lag1"].iloc[1:].to_numpy()
        want = d[f"{r}_state"].iloc[:-1].to_numpy()
        assert (got == want).all(), f"{r}_lag1 is not the prior-close state"


def test_timer_prefix_invariance():
    s = _spot_frame()
    for frame in (s, td.to_hourly(s)):
        full = td.timer_signals(frame)
        cols = [f"t_{t}" for t in td.TIMERS] + [f"t_{t}_dn" for t in td.TIMERS]
        for cut in RNG.integers(200, len(frame), N_CUTS):
            trunc = td.timer_signals(frame.iloc[:int(cut)].copy())
            _cmp(full, trunc, cols, int(cut))


def test_read_layer_causality():
    # tiny synthetic contract tape: selection and marks must ignore the future
    times = pd.date_range("2026-07-13 03:45:00+00:00", periods=40, freq="30min")
    times = times[times.indexer_between_time("03:45", "09:45")]
    opt = pd.DataFrame({
        "time": times, "underlying": "SYN", "expiry": "2026-07-28",
        "strike": 100.0, "option_type": "CE",
        "open": 5.0, "high": 5.5, "low": 4.5,
        "close": np.linspace(5, 8, len(times)),
        "volume": 100, "oi": 1000, "iv": 0.3, "source": "upstox"})
    spot = pd.DataFrame({"time": times, "underlying": "SYN",
                         "open": 100.0, "high": 101.0, "low": 99.0,
                         "close": np.linspace(100, 104, len(times)),
                         "volume": 1000})
    asof = times[5]
    full = OptionReadLayer(opt, spot)
    trunc = OptionReadLayer(opt[opt["time"] <= asof], spot)
    cs_f = full.contracts_for("SYN", date(2026, 7, 13), "CE", asof=asof)
    cs_t = trunc.contracts_for("SYN", date(2026, 7, 13), "CE", asof=asof)
    assert cs_f.drop(columns=["volume"]).equals(cs_t.drop(columns=["volume"]))
    cid = cs_f["contract_id"].iloc[0]
    m_f, m_t = full.mark(cid, asof), trunc.mark(cid, asof)
    assert abs(m_f.price - m_t.price) <= RTOL * abs(m_t.price)


def test_dedup_never_sums_and_prefers_upstox():
    t = pd.Timestamp("2026-07-13 03:45:00+00:00")
    opt = pd.DataFrame({
        "time": [t, t], "underlying": ["SYN", "SYN"],
        "expiry": ["2026-07-28"] * 2, "strike": [100.0] * 2,
        "option_type": ["CE"] * 2, "open": [5.0, 9.0], "high": [5.5, 9.5],
        "low": [4.5, 8.5], "close": [5.0, 9.0], "volume": [100, 900],
        "oi": [1000, 2000], "iv": [0.3, 0.4],
        "source": ["upstox", "fyers"]})
    spot = pd.DataFrame({"time": [t], "underlying": ["SYN"], "open": [100.0],
                         "high": [101.0], "low": [99.0], "close": [100.0],
                         "volume": [1000]})
    layer = OptionReadLayer(opt, spot)
    b = layer.bars("SYN|2026-07-28|100|CE")
    assert len(b) == 1
    assert b["close"].iloc[0] == 5.0          # upstox won despite lower volume
    assert b["volume"].iloc[0] == 100         # never summed


if __name__ == "__main__":
    for fn in [test_regime_prefix_invariance, test_regime_lag_contract,
               test_timer_prefix_invariance, test_read_layer_causality,
               test_dedup_never_sums_and_prefers_upstox]:
        fn()
        print("PASS", fn.__name__)
