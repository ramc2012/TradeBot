"""
Gann TP-Delta — full validation sweep through the Phase-1 gate harness.

Re-runs the conviction-floor sweep but pushes it through walk-forward + the six
overfitting gates (DSR/PBO/MinBTL/MC/WFE/regime) instead of a single in-sample
grid, to check the tuned floors (5.0 index / 6.0 commodity) hold OUT-OF-SAMPLE.

SAFETY: self-contained, ONE asyncpg connection, no app/broker bootstrap, reads
through the contamination guard (analysis.safe_candles). Run in the approved
gann-sweep SIDECAR (never the prod backend container):

  docker run --rm --network tradebot_default --memory=1200m \
    -v /opt/TradeBot/backend:/app -w /app tradebot-backend:latest \
    python gann_tp_delta/validate_sweep.py NIFTY BANKNIFTY SENSEX

Writes runtime/validation/gann_<sym>.json per underlying + prints a gate summary.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
import pandas as pd

from analysis.safe_candles import load_guarded_candles
from analysis.walk_forward import validate_strategy
from gann_tp_delta.backtest import GannTPDeltaBacktester
from gann_tp_delta.config import clone_default_config
from gann_tp_delta.tune_sweep import _build_frame

DSN = os.environ.get("SWEEP_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
LOOKBACK_DAYS = int(os.environ.get("SWEEP_DAYS", "420"))
FLOORS = [float(x) for x in os.environ.get("SWEEP_FLOORS", "4.0,4.5,5.0,5.5,6.0,6.5").split(",")]
OUT_DIR = Path(os.environ.get("SWEEP_OUT", "runtime/validation"))
# All sources by default — the guard cleans contamination, and the clean
# `timescaledb_spot_1minute` source is too shallow (~30d) for walk-forward.
SOURCE = os.environ.get("SWEEP_SOURCE") or None
# Windows sized for the available 1m depth (~110d); raise once backfill lands.
IS_DAYS = int(os.environ.get("SWEEP_IS_DAYS", "60"))
OOS_DAYS = int(os.environ.get("SWEEP_OOS_DAYS", "20"))
STRIDE_DAYS = int(os.environ.get("SWEEP_STRIDE_DAYS", "15"))

GRID = {"entry_conviction": FLOORS, "anchor_mode": ["auto_pivot"], "h_mode": ["median_tpd"]}


def _events_returns(res):
    return [float(t.get("r_multiple", 0.0)) for t in (res.get("events") or [])]


def _events_times(res):
    return [t.get("exit_time") for t in (res.get("events") or [])]


async def validate_underlying(conn, underlying: str) -> dict | None:
    cfg = clone_default_config()
    cfg.setdefault("backtest", {})["max_events"] = 100_000  # don't truncate the trade list
    raw = await load_guarded_candles(conn, underlying, interval="1minute", days=LOOKBACK_DAYS,
                                     source=SOURCE)
    if raw is None or len(raw) < 5000:
        print(f"{underlying:<11} SKIP — only {0 if raw is None else len(raw)} guarded 1m bars", flush=True)
        return None
    if "oi" not in raw.columns:
        raw["oi"] = 0.0  # gann _resample_15m aggregates oi; index spot has none
    frame = _build_frame(raw, cfg)
    bt = GannTPDeltaBacktester(cfg)

    def run_fn(fr, p):
        return bt.run(fr, anchor_mode=p["anchor_mode"], h_mode=p["h_mode"], entry_conviction=p["entry_conviction"])

    daily_closes = frame.set_index(pd.to_datetime(frame["time"], utc=True))["close"].resample("1D").last().dropna()

    report = validate_strategy(
        frame, run_fn, GRID, _events_returns,
        extract_exit_times=_events_times, daily_closes=daily_closes,
        time_col="time", is_days=IS_DAYS, oos_days=OOS_DAYS, stride_days=STRIDE_DAYS,
        select="total", target_sr_annual=1.0,
    )
    report["underlying"] = underlying
    report["lookback_days"] = LOOKBACK_DAYS
    report["guarded_bars"] = int(len(frame))
    return report


def _print_summary(rep: dict):
    u = rep["underlying"]
    g = rep["gates"]
    wf = rep["walk_forward"]
    print(f"\n=== {u} — verdict {rep['verdict']} ({rep['gates_passed']}/{rep['gates_total']}) ===", flush=True)
    print(f"  OOS trades {rep['metrics']['trades']} · WFE {wf['wfe_median']} · DSR {rep['deflated_sharpe']:.3f} "
          f"· PBO {rep['pbo'].get('pbo')} · MC_SR_p05 {rep['monte_carlo'].get('sharpe_p05')} "
          f"· MinBTL {rep['min_backtest_length_years']:.1f}y vs have {rep['backtest_years']}y", flush=True)
    # which floor each window picked (does 5.0/6.0 recur OOS?)
    picks = [w["best_params"]["entry_conviction"] for w in wf["per_window"]]
    print(f"  per-window IS-best floors: {picks}", flush=True)
    for k, gg in g.items():
        print(f"    {k:<18} {'PASS' if gg['pass'] else 'fail'}  {gg.get('value')}", flush=True)


async def main():
    underlyings = sys.argv[1:] or ["NIFTY", "BANKNIFTY", "SENSEX"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(DSN)
    print(f"=== Gann validation sweep (lookback {LOOKBACK_DAYS}d, floors {FLOORS}) ===", flush=True)
    try:
        for u in underlyings:
            try:
                rep = await validate_underlying(conn, u)
            except Exception as exc:  # noqa: BLE001
                print(f"{u:<11} ERROR {exc}", flush=True)
                continue
            if rep is None:
                continue
            (OUT_DIR / f"gann_{u}.json").write_text(json.dumps(rep, indent=1, default=str))
            _print_summary(rep)
    finally:
        await conn.close()
    print("\n=== validation sweep done ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
