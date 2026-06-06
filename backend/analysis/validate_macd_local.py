"""
Spot-MACD validation on the LOCAL 5y Alpaca dataset — the first walk-forward run
with enough depth to SATISFY MinBTL (the NSE 82-day history could not). Runs
entirely off-prod (local parquet), so it uses the approved local compute lane.

This validates the HARNESS end-to-end at statistical adequacy and gives an honest
gate verdict for a frequent, instrument-agnostic trend strategy (the NSE-S1 family
mirrored on the underlying). Parameter values are SPY-specific; the transferable
result is whether the methodology survives the six overfitting gates at depth.

  python -m analysis.validate_macd_local SPY QQQ DIA
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

from analysis.alpaca_data import load_alpaca_rth
from analysis.safe_candles import guard_ohlc
from analysis.spot_macd_backtest import macd_backtest
from analysis.walk_forward import validate_strategy

OUT_DIR = Path(os.environ.get("MACD_OUT", "runtime/validation"))
TF = os.environ.get("MACD_TF", "30min")
IS_DAYS = int(os.environ.get("MACD_IS_DAYS", "504"))      # 2y
OOS_DAYS = int(os.environ.get("MACD_OOS_DAYS", "126"))    # 6mo
STRIDE_DAYS = int(os.environ.get("MACD_STRIDE_DAYS", "126"))

# Focused grid — kept small so n_trials (and therefore MinBTL) stays sane.
GRID = {
    "fast": [8, 12],
    "slow": [21, 26],
    "sl_atr": [1.0, 1.5],
    "tp_atr": [2.0, 3.0],
    "signal": [9],
    "tf": [TF],
}


def _ret(res):
    return [float(e.get("r_multiple", 0.0)) for e in (res.get("events") or [])]


def _times(res):
    return [e.get("exit_time") for e in (res.get("events") or [])]


def validate(symbol: str) -> dict | None:
    raw = load_alpaca_rth(symbol)
    if raw.empty or len(raw) < 20000:
        print(f"{symbol}: SKIP — {len(raw)} RTH 1m bars", flush=True)
        return None
    raw = guard_ohlc(raw, rth=False)
    span = (raw["time"].max() - raw["time"].min()).days
    print(f"{symbol}: {len(raw)} RTH 1m bars, span {span}d ({span/365.25:.2f}y), tf {TF}; "
          f"IS{IS_DAYS}/OOS{OOS_DAYS}/stride{STRIDE_DAYS}", flush=True)

    def run_fn(fr, p):
        return macd_backtest(fr, fast=p["fast"], slow=p["slow"], signal=p["signal"], tf=p["tf"],
                             sl_atr=p["sl_atr"], tp_atr=p["tp_atr"])

    daily_closes = raw.set_index(pd.to_datetime(raw["time"], utc=True))["close"].resample("1D").last().dropna()
    rep = validate_strategy(
        raw, run_fn, GRID, _ret, extract_exit_times=_times, daily_closes=daily_closes,
        time_col="time", is_days=IS_DAYS, oos_days=OOS_DAYS, stride_days=STRIDE_DAYS,
        select="total", target_sr_annual=1.0,
    )
    rep["symbol"] = symbol
    rep["strategy"] = "spot_macd"
    rep["timeframe"] = TF
    return rep


def _summary(rep: dict):
    g = rep["gates"]; wf = rep["walk_forward"]
    print(f"\n=== {rep['symbol']} spot-MACD {rep['timeframe']} — verdict {rep['verdict']} "
          f"({rep['gates_passed']}/{rep['gates_total']}) ===", flush=True)
    print(f"  OOS trades {rep['metrics']['trades']} | WFE {wf['wfe_median']} | DSR {rep['deflated_sharpe']:.3f} "
          f"| PBO {rep['pbo'].get('pbo')} | MC_SR_p05 {rep['monte_carlo'].get('sharpe_p05')} "
          f"| MinBTL {rep['min_backtest_length_years']:.1f}y vs have {rep['backtest_years']}y "
          f"| n_trials {rep['n_trials']} | windows {wf['n_windows']}", flush=True)
    print(f"  per-window IS-best params: {[w['best_params'] for w in wf['per_window']]}", flush=True)
    print("  OOS metrics:", {k: rep['metrics'].get(k) for k in ('trades', 'sharpe', 'total', 'win_rate', 'profit_factor', 'expectancy')}, flush=True)
    for k, gg in g.items():
        print(f"    {k:<18} {'PASS' if gg['pass'] else 'fail'}  {gg.get('value')}", flush=True)


def main():
    syms = sys.argv[1:] or ["SPY"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== spot-MACD LOCAL validation (Alpaca 5y 1m, tf {TF}) grid {GRID} ===", flush=True)
    for s in syms:
        try:
            rep = validate(s)
        except Exception as exc:  # noqa: BLE001
            import traceback; traceback.print_exc(); print(f"{s}: ERROR {exc}", flush=True); continue
        if rep is None:
            continue
        (OUT_DIR / f"macd_{s}_local.json").write_text(json.dumps(rep, indent=1, default=str))
        _summary(rep)
    print("\n=== done ===", flush=True)


if __name__ == "__main__":
    main()
