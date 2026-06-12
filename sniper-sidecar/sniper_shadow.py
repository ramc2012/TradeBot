"""Sniper shadow lane — run the ExcursionEstimator on live bars + AI order flow; log predictions.

ISOLATED from the trading loop (observes only). Pulls recent minute bars + the live
OrderFlowSnapshot from auction_intelligence.live, runs the estimator (incl. real OF family B2 fed
by the live snapshot during market hours), and appends each prediction to a JSONL log for later
scoring vs realized outcomes (retrain on REAL order flow). Self-heals its pip deps so it survives
container recreates until they're baked into the image.

Run:  cd /app && python sniper_shadow.py NIFTY        (or ALL)
"""
import importlib.util, subprocess, sys
for _pkg in ("lightgbm", "tqdm", "joblib"):
    if importlib.util.find_spec(_pkg) is None:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", _pkg], check=False)

import asyncio, json
import pandas as pd
from nomad_sniper.integration.ai_lane import SniperEstimatorLane
from nomad_sniper.utils.normalize import atr_reference
from auction_intelligence.live import _fetch_recent_minute_rows, build_live_analysis

MODEL = "/app/sniper_artifacts/excursion_estimator_sensex.joblib"
LOG = "/app/sniper_shadow.jsonl"
SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX")
_LANE = SniperEstimatorLane(MODEL, shadow_sink=lambda r: open(LOG, "a").write(json.dumps(r) + "\n"))


def _bars(rows):
    df = pd.DataFrame(rows); df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    df.index = df.index.tz_localize("Asia/Kolkata") if df.index.tz is None else df.index.tz_convert("Asia/Kolkata")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


async def run_once(symbol):
    try:
        rows, src, _ = await _fetch_recent_minute_rows(symbol)
        bars = _bars(rows)
        atr = atr_reference(bars, bars.index[-1].date())
        of = {}
        try:
            of = (await build_live_analysis(symbol)).get("order_flow") or {}
        except Exception:
            pass
        pred = _LANE.predict(decision_time=bars.index[-1], bars=bars, atr_ref=atr, of_snapshot=of, symbol=symbol)
        print(f"{symbol}: bars={len(bars)} has_of={bool(of)} 1d_signed={pred.get('1d',{}).get('signed_move')}")
    except Exception as e:
        print(f"{symbol}: ERR {repr(e)[:100]}")


async def main(symbols):
    for s in symbols:
        await run_once(s)

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "NIFTY"
    syms = SYMBOLS if arg.upper() == "ALL" else (arg,)
    asyncio.run(main(syms))
