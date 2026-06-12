"""Regression tests for the Black-76 GEX engine vs the fyers-webapp reference.

Golden values were captured from `reference/py/compute3.py` run on its embedded
real NIFTY chains (16-Jun expiry, fp=23440, spot=23366.7, T=0.026301):
  ATM 23450 · ATM-IV 15.8 · max-pain 23450 · walls C23500/P23500 ·
  net-GEX -103.01 · net-DEX 7.43 · gamma-flip 23494 · PCR 0.978
"""
from __future__ import annotations

import math

from directional_options.gex_engine import (
    black76_price,
    build_term_structure,
    compute_expiry_gex,
    compute_progression,
    greeks,
    implied_vol,
)

# 16-Jun NIFTY chain: strike -> [ce_ltp, ce_oi, pe_ltp, pe_oi]
_CH16_RAW = {
    22750: [734.9, 2015, 43.65, 707265], 22800: [690, 7215, 50.45, 735800],
    22850: [652.7, 4030, 58.5, 179530], 22900: [605.1, 15470, 66.6, 405080],
    22950: [570.2, 6565, 76.8, 117455], 23000: [523.8, 131820, 87, 1040845],
    23050: [491.45, 14040, 99.8, 121225], 23100: [444.2, 67080, 114.55, 335595],
    23150: [412.45, 35360, 127.15, 90285], 23200: [375.1, 202085, 144, 578760],
    23250: [340.65, 71435, 159.75, 122720], 23300: [318.35, 732290, 177.7, 881335],
    23350: [283.25, 104130, 197, 200330], 23400: [255.9, 808145, 222.55, 864825],
    23450: [232.1, 204750, 246.6, 142025], 23500: [209.5, 1490320, 269.1, 1078740],
    23550: [183.25, 150150, 295.75, 95550], 23600: [164, 644930, 330.3, 441935],
    23650: [145.75, 107445, 354.8, 73320], 23700: [131.45, 454935, 390, 145730],
    23750: [114, 152815, 415.1, 16575], 23800: [100, 875550, 458.2, 165425],
    23850: [87.95, 193180, 496.8, 14105], 23900: [74.8, 849810, 534.15, 105560],
    23950: [66, 124930, 562.55, 12675],
}
_SPOT = 23366.7
_T = 0.026301
_FWD = 23440.0
_TOTALS = (20090655, 19656455)


def _by_strike(raw):
    return {
        K: {"ce_ltp": v[0], "ce_oi": v[1], "pe_ltp": v[2], "pe_oi": v[3]}
        for K, v in raw.items()
    }


def test_matches_reference_golden_with_explicit_forward():
    out = compute_expiry_gex(
        _by_strike(_CH16_RAW), _SPOT, _T,
        forward=_FWD, totals=_TOTALS, expiry_label="16-Jun",
    )
    m = out["meta"]
    assert m["atm"] == 23450
    assert m["max_pain"] == 23450
    assert m["call_wall"] == 23500
    assert m["put_wall"] == 23500
    assert m["gamma_flip"] == 23494
    assert abs(m["atm_iv"] - 15.8) < 0.05
    assert abs(m["pcr"] - 0.978) < 0.002
    assert abs(m["net_gex"] - (-103.01)) < 0.5
    assert abs(m["net_dex"] - 7.43) < 0.5
    assert len(out["rows"]) == 25
    # Per-strike row integrity: GEX present and finite, IV in a sane band.
    atm_row = next(r for r in out["rows"] if r["strike"] == 23450)
    assert atm_row["ce_iv"] is not None and 5 < atm_row["ce_iv"] < 40
    assert math.isfinite(atm_row["gex"])


def test_implied_forward_lands_near_true_forward():
    # No explicit forward -> implied from ATM parity. Should land within a few
    # points of the reference fp (23440) and produce finite, sane outputs.
    out = compute_expiry_gex(_by_strike(_CH16_RAW), _SPOT, _T, totals=_TOTALS)
    m = out["meta"]
    assert abs(m["fp"] - _FWD) < 30
    assert m["max_pain"] == 23450
    assert m["gamma_flip"] is not None
    assert math.isfinite(m["net_gex"])


def test_black76_put_call_parity():
    F, K, T, s, r = 23440.0, 23400.0, 0.05, 0.16, 0.065
    c = black76_price(F, K, T, s, r, "C")
    p = black76_price(F, K, T, s, r, "P")
    # C - P = e^{-rT}(F - K)
    assert abs((c - p) - math.exp(-r * T) * (F - K)) < 1e-6


def test_implied_vol_recovers_input_sigma():
    F, K, T, r = 23440.0, 23500.0, 0.05, 0.065
    for sigma in (0.10, 0.18, 0.30):
        price = black76_price(F, K, T, sigma, r, "C")
        recovered = implied_vol(price, F, K, T, r, "C")
        assert abs(recovered - sigma) < 1e-3


def test_deep_itm_iv_is_nan():
    # Premium ≈ intrinsic (deep ITM near expiry) -> NaN IV by design.
    F, K, T, r = 23440.0, 22000.0, 0.01, 0.065
    intrinsic = math.exp(-r * T) * (F - K)
    assert math.isnan(implied_vol(intrinsic, F, K, T, r, "C"))


def test_gamma_flip_none_when_no_crossing():
    # All-call chain (no puts) -> net GEX strictly positive, no zero crossing.
    raw = {23400: [100, 5000, 0.05, 0], 23500: [60, 5000, 0.05, 0], 23600: [30, 5000, 0.05, 0]}
    out = compute_expiry_gex(_by_strike(raw), _SPOT, _T, forward=_FWD)
    assert out["meta"]["gamma_flip"] is None


def test_term_structure_orders_and_aggregates():
    metas = [
        {"expiry": "wk1", "days": 5, "pcr": 0.9, "atm_iv": 16, "net_gex": -100,
         "max_pain": 23400, "tot_ce_oi": 1e7, "tot_pe_oi": 1e7},
        {"expiry": "wk2", "days": 12, "pcr": 1.1, "atm_iv": 15, "net_gex": 50,
         "max_pain": 23500, "tot_ce_oi": 2e7, "tot_pe_oi": 1e7},
    ]
    term = build_term_structure(metas)
    assert term["labels"] == ["wk1", "wk2"]
    assert term["atm_iv"] == [16, 15]
    assert term["tot_oi"] == [2.0, 3.0]


def test_empty_chain_is_safe():
    out = compute_expiry_gex({}, _SPOT, _T, forward=_FWD)
    assert out["rows"] == []
    assert out["meta"]["net_gex"] is None


def test_progression_shapes_and_regime():
    times = ["09:30", "10:00", "10:30"]
    spot = [23400.0, 23380.0, 23420.0]
    T = [0.025, 0.0249, 0.0248]
    series = {
        23400: {"ce_close": [120, 110, 130], "ce_oi": [500000, 520000, 510000],
                "pe_close": [115, 125, 105], "pe_oi": [480000, 470000, 490000]},
        23500: {"ce_close": [70, 62, 78], "ce_oi": [800000, 810000, 805000],
                "pe_close": [170, 182, 158], "pe_oi": [300000, 305000, 302000]},
    }
    prog = compute_progression(series, times, spot, T)
    assert prog["strikes"] == [23400, 23500]
    assert len(prog["gex"]) == 3
    assert len(prog["gdens"]) == 2 and len(prog["gdens"][0]) == 3   # strike × time matrix
    assert len(prog["oi_call"]) == 2 and len(prog["oi_put"]) == 2
    assert all(reg in ("pos", "neg") for reg in prog["regime"])
    assert prog["atm"] in (23400, 23500)


def test_progression_tolerates_missing_buckets():
    times = ["09:30", "10:00"]
    series = {23400: {"ce_close": [120, None], "ce_oi": [500000, None],
                      "pe_close": [115, None], "pe_oi": [480000, None]}}
    prog = compute_progression(series, times, [23400.0, None], [0.025, None])
    assert prog["gex"][1] is None and prog["regime"][1] is None


def test_progression_emits_none_not_zero_for_empty_strikes():
    """Regression (2026-06-10): strikes with NO data in a bucket rendered as
    gdens 0.0 — the heatmap painted no-data as zero gamma (5 of 7 band strikes
    were empty)."""
    times = ["09:30", "10:00"]
    series = {
        23400: {"ce_close": [120, 110], "ce_oi": [500000, 510000],
                "pe_close": [115, 125], "pe_oi": [480000, 470000]},
        23500: {"ce_close": [None, None], "ce_oi": [None, None],
                "pe_close": [None, None], "pe_oi": [None, None]},
    }
    prog = compute_progression(series, times, [23400.0, 23410.0], [0.025, 0.024])
    empty_idx = prog["strikes"].index(23500)
    data_idx = prog["strikes"].index(23400)
    assert all(v is None for v in prog["gdens"][empty_idx])
    assert all(v is None for v in prog["netgex"][empty_idx])
    assert all(v is not None for v in prog["gdens"][data_idx])


def test_progression_signed_netgex_matrix_and_oi_change():
    """The spec's Net-GEX strike×time heatmap needs SIGNED per-strike GEX
    (call gamma +, put gamma −) and a bucket-over-bucket ΔOI series."""
    times = ["09:30", "10:00"]
    put_heavy = {
        23400: {"ce_close": [None, None], "ce_oi": [None, None],
                "pe_close": [115, 125], "pe_oi": [480000, 500000]},
    }
    prog = compute_progression(put_heavy, times, [23400.0, 23410.0], [0.025, 0.024])
    # Put-only strike → signed GEX strictly negative wherever computed.
    assert all(v < 0 for v in prog["netgex"][0] if v is not None)
    # Band OI 480k → 500k → ΔOI[0] None, ΔOI[1] = +20000.
    assert prog["oi_change"][0] is None
    assert prog["oi_change"][1] == 20000


def test_snapshot_pseudo_candle_detection():
    from directional_options.gex_progression import _looks_like_snapshot_rows

    flat = [{"open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0} for _ in range(20)]
    real = [{"open": 5.0, "high": 5.4, "low": 4.8, "close": 5.2} for _ in range(20)]
    assert _looks_like_snapshot_rows(flat) is True
    assert _looks_like_snapshot_rows(real) is False
    assert _looks_like_snapshot_rows([]) is False


import pytest


@pytest.mark.asyncio
async def test_leg_candles_prefers_resampled_3m_over_snapshot_30m(monkeypatch):
    """Regression (2026-06-10): the front weekly had ZERO native 30m candles,
    so the 30m load silently degraded to LTP pseudo-candles while dense REAL
    3-minute candles existed. The leg loader must resample 3m → 30m first."""
    import directional_options.gex_progression as prog_mod
    from datetime import date as _date

    async def fake_load_candles(*, interval, limit, **_kw):
        if interval == "3minute":
            from datetime import datetime, timedelta
            t0 = datetime(2026, 6, 10, 9, 15)
            return [
                {
                    "time": (t0 + timedelta(minutes=3 * i)).isoformat() + "+05:30",
                    "open": 10.0 + i * 0.1, "high": 10.5 + i * 0.1,
                    "low": 9.8 + i * 0.1, "close": 10.2 + i * 0.1,
                    "volume": 100, "oi": 1000 + i,
                }
                for i in range(45)
            ]
        # Native 30m: only flat snapshot pseudo-candles.
        return [{"time": "2026-06-10T10:00:00+05:30", "open": 5.0, "high": 5.0,
                 "low": 5.0, "close": 5.0, "oi": 10} for _ in range(5)]

    monkeypatch.setattr(prog_mod.option_history_service, "load_candles", fake_load_candles)
    rows, source = await prog_mod._leg_candles(
        "NIFTY", _date(2026, 6, 16), 23400.0, "CE", interval="30minute", limit=80
    )
    assert source == "resampled_3m"
    assert len(rows) >= 2
    # Aggregated buckets must carry real OHLC variation, not flat LTP.
    assert any(r["high"] != r["low"] for r in rows)
