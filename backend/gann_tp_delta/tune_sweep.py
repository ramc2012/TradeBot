"""Offline Gann conviction-floor tuning sweep.

Runs OFF the database in an ISOLATED process — never inside the prod backend
container (heavy backtests there OOM it, which recreates the container, reverts
the bind mount, and leaks DB connections). This script:

  * connects directly via asyncpg (ONE connection, no app/broker bootstrap),
  * rebuilds the 15-minute feature frame with the SAME compute_ema / compute_adx
    the live FeatureEngine uses (so signals match production),
  * runs the event-driven backtester at a range of entry-conviction floors,

and prints expectancy / profit-factor / trades per (underlying, floor) so the
commodity_min_conviction can be picked from data instead of guessed.

Run (isolated, memory-capped sidecar):
  docker run -d --name gann-sweep --network tradebot_default --memory=1200m \
    -v /opt/TradeBot/backend:/app -w /app tradebot-backend \
    python gann_tp_delta/tune_sweep.py
  docker logs -f gann-sweep
"""
from __future__ import annotations

import asyncio
import os

import asyncpg
import pandas as pd

from analysis.macd_engine import compute_ema
from analytics.technicals import compute_adx
from gann_tp_delta.backtest import GannTPDeltaBacktester
from gann_tp_delta.config import clone_default_config

DSN = os.environ.get("SWEEP_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL", "GOLD", "NATURALGAS", "SILVERM"]
FLOORS = [4.0, 4.5, 5.0, 5.5, 6.0, 6.5]
LOOKBACK_DAYS = int(os.environ.get("SWEEP_LOOKBACK_DAYS", "150"))


def _resample_15m(df: pd.DataFrame) -> pd.DataFrame:
    indexed = df.set_index("time").sort_index()
    return (
        indexed.resample("15min", label="right", closed="right")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last"})
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    pc = frame["close"].shift(1)
    tr = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - pc).abs(), (frame["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean().fillna(0.0)


def _build_frame(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    f = _resample_15m(df)
    if f.empty or len(f.index) < 60:
        return f
    fe = cfg["feature_engine"]
    closes, highs, lows = f["close"].tolist(), f["high"].tolist(), f["low"].tolist()
    f["ema_fast"] = pd.Series(compute_ema(closes, int(fe["ema_fast"])), dtype="float64")
    f["ema_slow"] = pd.Series(compute_ema(closes, int(fe["ema_slow"])), dtype="float64")
    adx, _, _ = compute_adx(highs, lows, closes, int(fe["adx_period"]))
    f["adx"] = pd.Series(adx, dtype="float64")
    f["atr"] = _atr(f, int(fe["atr_period"]))
    warmup = int(fe["warmup_bars"])
    if len(f.index) > warmup:
        f = f.iloc[warmup:].reset_index(drop=True)
    return f


async def _candles(conn: asyncpg.Connection, underlying: str, days: int) -> pd.DataFrame:
    rows = await conn.fetch(
        """
        SELECT time, open, high, low, close, COALESCE(volume, 0) AS volume
        FROM underlying_spot_candles
        WHERE underlying = $1 AND interval = '1minute'
          AND time >= NOW() - ($2 || ' days')::interval
        ORDER BY time ASC
        """,
        underlying, str(int(days)),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["oi"] = 0
    return df.dropna(subset=["open", "high", "low", "close"])


async def main() -> None:
    cfg = clone_default_config()
    bt = GannTPDeltaBacktester(cfg)
    conn = await asyncpg.connect(DSN)
    print(f"=== Gann conviction-floor sweep (lookback {LOOKBACK_DAYS}d) ===", flush=True)
    print("%-11s %-5s | %6s %6s %7s %6s %7s %7s" % ("underlying", "floor", "trades", "win%", "expR", "PF", "totR", "maxDD"), flush=True)
    print("-" * 66, flush=True)
    try:
        for u in UNDERLYINGS:
            df = await _candles(conn, u, LOOKBACK_DAYS)
            if df.empty:
                print(f"{u:<11} NO DATA", flush=True)
                continue
            frame = _build_frame(df, cfg)
            if frame.empty:
                print(f"{u:<11} THIN ({len(df)} 1m bars)", flush=True)
                continue
            for fl in FLOORS:
                s = bt.run(frame, anchor_mode="auto_pivot", h_mode="median_tpd", entry_conviction=fl)["summary"]
                pf = s["profit_factor"]
                print("%-11s %-5.1f | %6s %6s %7s %6s %7s %7s" % (
                    u, fl, s["trades"], s["win_rate_pct"], s["expectancy_r"],
                    (pf if pf is not None else "-"), s["total_r"], s["max_drawdown_r"]), flush=True)
            print("-" * 66, flush=True)
    finally:
        await conn.close()
    print("=== sweep done ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
