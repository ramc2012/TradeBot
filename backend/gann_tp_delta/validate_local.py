"""
Gann validation on a LOCAL deep dataset (5y 1-min Alpaca bars) — runs entirely
off-prod. With ~5y of data the walk-forward windows reach IS=2y/OOS=6mo and the
MinBTL gate is satisfiable, so this is the first STATISTICALLY ADEQUATE OOS read.

Strategies are instrument-agnostic (Gann geometry on relative price/ATR), so SPY
validates the methodology + structural edge at depth; parameter values won't
transfer 1:1 to NSE, but plateau/stability behaviour does.

  python -m gann_tp_delta.validate_local SPY QQQ
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import time as dtime
from pathlib import Path

import pandas as pd

from analysis.safe_candles import guard_ohlc
from analysis.walk_forward import validate_strategy
from gann_tp_delta.backtest import GannTPDeltaBacktester
from gann_tp_delta.config import clone_default_config
from gann_tp_delta.tune_sweep import _build_frame

DATA_DIR = os.environ.get("ALPACA_DIR", "/Users/chinnadurairamachandran/Claude Projects/TradingBot/alpaca data/data")
FLOORS = [float(x) for x in os.environ.get("SWEEP_FLOORS", "4.0,4.5,5.0,5.5,6.0,6.5").split(",")]
OUT_DIR = Path(os.environ.get("SWEEP_OUT", "runtime/validation"))
IS_DAYS = int(os.environ.get("SWEEP_IS_DAYS", "504"))     # 2y
OOS_DAYS = int(os.environ.get("SWEEP_OOS_DAYS", "126"))   # 6mo
STRIDE_DAYS = int(os.environ.get("SWEEP_STRIDE_DAYS", "126"))

GRID = {"entry_conviction": FLOORS, "anchor_mode": ["auto_pivot"], "h_mode": ["median_tpd"]}


def load_alpaca_rth(symbol: str) -> pd.DataFrame:
    files = sorted(glob.glob(f"{DATA_DIR}/{symbol}/bars_1min/*.parquet"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(f) for f in files])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.reset_index().rename(columns={df.index.name or "index": "time"})
    if "time" not in df.columns:
        df = df.rename(columns={df.columns[0]: "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    et = df["time"].dt.tz_convert("America/New_York")
    df = df[(et.dt.time >= dtime(9, 30)) & (et.dt.time < dtime(16, 0))].reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["oi"] = 0.0
    return df[["time", "open", "high", "low", "close", "volume", "oi"]]


def _ret(res):
    return [float(t.get("r_multiple", 0.0)) for t in (res.get("events") or [])]


def _times(res):
    return [t.get("exit_time") for t in (res.get("events") or [])]


def validate(symbol: str) -> dict | None:
    raw = load_alpaca_rth(symbol)
    if raw.empty or len(raw) < 20000:
        print(f"{symbol}: SKIP — {len(raw)} RTH 1m bars", flush=True)
        return None
    raw = guard_ohlc(raw, rth=False)  # US RTH already applied; keep dedup + magnitude guard
    cfg = clone_default_config()
    cfg.setdefault("backtest", {})["max_events"] = 1_000_000
    frame = _build_frame(raw, cfg)
    bt = GannTPDeltaBacktester(cfg)
    span_days = (pd.to_datetime(frame["time"]).max() - pd.to_datetime(frame["time"]).min()).days
    print(f"{symbol}: {len(raw)} RTH 1m bars -> {len(frame)} 15m bars, span {span_days}d "
          f"({span_days/365.25:.2f}y); windows IS{IS_DAYS}/OOS{OOS_DAYS}/stride{STRIDE_DAYS}", flush=True)

    def run_fn(fr, p):
        return bt.run(fr, anchor_mode=p["anchor_mode"], h_mode=p["h_mode"], entry_conviction=p["entry_conviction"])

    daily_closes = frame.set_index(pd.to_datetime(frame["time"], utc=True))["close"].resample("1D").last().dropna()
    report = validate_strategy(
        frame, run_fn, GRID, _ret, extract_exit_times=_times, daily_closes=daily_closes,
        time_col="time", is_days=IS_DAYS, oos_days=OOS_DAYS, stride_days=STRIDE_DAYS,
        select="total", target_sr_annual=1.0,
    )
    report["symbol"] = symbol
    report["data"] = "alpaca_1m_rth"
    return report


def _summary(rep: dict):
    g = rep["gates"]; wf = rep["walk_forward"]
    print(f"\n=== {rep['symbol']} — verdict {rep['verdict']} ({rep['gates_passed']}/{rep['gates_total']}) ===", flush=True)
    print(f"  OOS trades {rep['metrics']['trades']} | WFE {wf['wfe_median']} | DSR {rep['deflated_sharpe']:.3f} "
          f"| PBO {rep['pbo'].get('pbo')} | MC_SR_p05 {rep['monte_carlo'].get('sharpe_p05')} "
          f"| MinBTL {rep['min_backtest_length_years']:.1f}y vs have {rep['backtest_years']}y "
          f"| n_trials {rep['n_trials']} | windows {wf['n_windows']}", flush=True)
    print(f"  per-window IS-best floors: {[w['best_params']['entry_conviction'] for w in wf['per_window']]}", flush=True)
    print("  in-sample floor sweep:", flush=True)
    for r in rep.get("sweep", {}).get("summary_table", []):
        pf = r.get("profit_factor", 0); pf = 99.0 if pf == float("inf") else pf
        print("    floor %-4.1f  trades %4d  win %4.1f%%  totalR %7.2f  PF %5.2f  expR %6.3f"
              % (r.get("entry_conviction", 0), r.get("trades", 0), 100 * r.get("win_rate", 0), r.get("total", 0), pf, r.get("expectancy", 0)), flush=True)
    for k, gg in g.items():
        print(f"    {k:<18} {'PASS' if gg['pass'] else 'fail'}  {gg.get('value')}", flush=True)


def main():
    syms = sys.argv[1:] or ["SPY"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== Gann LOCAL validation (Alpaca 5y 1m) floors {FLOORS} ===", flush=True)
    for s in syms:
        try:
            rep = validate(s)
        except Exception as exc:  # noqa: BLE001
            import traceback; traceback.print_exc(); print(f"{s}: ERROR {exc}", flush=True); continue
        if rep is None:
            continue
        (OUT_DIR / f"gann_{s}_local.json").write_text(json.dumps(rep, indent=1, default=str))
        _summary(rep)
    print("\n=== done ===", flush=True)


if __name__ == "__main__":
    main()
