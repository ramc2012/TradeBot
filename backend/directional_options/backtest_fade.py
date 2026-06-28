"""Trade-level OPTION backtest of the intraday FADE entry -> equity curve.

This is the net-of-cost, option-level test (where theta + spread actually bite).
One fade trade per session: at the strongest open-window extension, BUY the ATM
option on the fade side (up-stretch -> PE, down-stretch -> CE), hold intraday with
a same-day exit ladder (trail-from-peak / -35% stop / EOD), book P&L NET of
round-trip charges + bid/ask spread. Produces cumulative equity for:
  * fade (low-IV gated)   — the strategy
  * fade (all entries)    — ungated, to show the IV gate's value
  * momentum (opposite side) — the baseline the lane runs today

Pure research on option_premium_candles (30-min, full greeks). Prints a JSON
block (equity curves + stats) for rendering. Run:
  python -m directional_options.backtest_fade
"""
from __future__ import annotations

import asyncio
import json
from datetime import time, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_START = time(9, 15)
SESSION_END = time(15, 30)
LOT = {"NIFTY": 75, "BANKNIFTY": 35}
ATR_WIN = 14
OPEN_WINDOW_BARS = 4      # first 4 x 30-min bars (~09:15-11:15)
EXT_GATE = 1.0            # |ext| >= 1 ATR to act
STOP = 0.35              # -35% premium hard stop
TRAIL_ARM = 0.20         # arm trail once +20% in profit
TRAIL_GIVEBACK = 0.20    # exit on giveback from peak
TARGET = 0.40            # +40% take-profit (capture the convex snap-back pop)
TIME_STOP_BARS = 2       # fade is a 30-60min reversion: don't hold past ~60min (theta)
PER_SIDE_SPREAD = 0.005  # 0.5% of premium per leg (realistic ATM index weekly)
IV_GATE_PCT = 0.50       # enter only when ATM-IV expanding-percentile <= this


async def _load(underlying: str) -> pd.DataFrame:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT time, expiry, strike, option_type, close, delta, iv, underlying_price
                    FROM option_premium_candles
                    WHERE underlying = :u AND interval = '30minute'
                      AND close IS NOT NULL AND close > 0 AND delta IS NOT NULL
                      AND underlying_price IS NOT NULL
                    ORDER BY time
                    """
                ),
                {"u": underlying},
            )
        ).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "expiry", "strike", "option_type", "close", "delta", "iv", "spot"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(IST)
    for c in ("strike", "close", "delta", "iv", "spot"):
        df[c] = df[c].astype(float)
    df["date"] = df["time"].dt.date
    t = df["time"].dt.time
    df = df[(t >= SESSION_START) & (t <= SESSION_END)]
    return df


def _round_trip_cost(entry: float, exit_: float, qty: int, underlying: str) -> float:
    try:
        from paper_engine.costs import round_trip_charges
        chg = round_trip_charges(
            symbol=underlying, instrument_type="CE", entry_price=entry,
            exit_price=exit_, qty=qty, entry_action="BUY",
        )
    except Exception:
        chg = 0.0
    spread = (abs(entry) + abs(exit_)) * PER_SIDE_SPREAD * qty
    return chg + spread


def _atr(spot_by_bar: pd.Series, win: int) -> pd.Series:
    # ATR proxy on the 30-min underlying path (no per-bar high/low here, use |Δ|).
    diff = spot_by_bar.diff().abs()
    return diff.rolling(win, min_periods=max(2, win // 2)).mean()


def _backtest(df: pd.DataFrame, underlying: str) -> dict:
    lot = LOT.get(underlying, 75)
    # Underlying path: one spot per (date,time).
    spot = df.groupby(["date", "time"])["spot"].first().reset_index().sort_values("time")
    spot["atr"] = _atr(spot["spot"], ATR_WIN)
    trades_fade, trades_mom = [], []
    iv_hist: list[float] = []

    for d, day_spot in spot.groupby("date"):
        day_spot = day_spot.sort_values("time").reset_index(drop=True)
        if len(day_spot) < OPEN_WINDOW_BARS + 2:
            continue
        sess_open = day_spot["spot"].iloc[0]
        ow = day_spot.iloc[:OPEN_WINDOW_BARS].copy()
        ow["ext"] = (ow["spot"] - sess_open) / ow["atr"].replace(0, np.nan)
        ow = ow.dropna(subset=["ext"])
        if ow.empty:
            continue
        row = ow.loc[ow["ext"].abs().idxmax()]
        if abs(row["ext"]) < EXT_GATE:
            continue
        entry_time = row["time"]
        fade_side = "PE" if row["ext"] > 0 else "CE"
        mom_side = "CE" if row["ext"] > 0 else "PE"

        def _run(side: str) -> tuple[float, float] | None:
            day_opt = df[(df["date"] == d) & (df["option_type"] == side)]
            at_entry = day_opt[day_opt["time"] == entry_time]
            if at_entry.empty:
                return None
            # front expiry, ATM by |delta|~0.5
            exp = at_entry["expiry"].min()
            cand = at_entry[at_entry["expiry"] == exp].copy()
            cand["dd"] = (cand["delta"].abs() - 0.5).abs()
            pick = cand.loc[cand["dd"].idxmin()]
            strike, entry_px, entry_iv = pick["strike"], float(pick["close"]), float(pick["iv"])
            if entry_px <= 0:
                return None
            path = day_opt[(day_opt["expiry"] == exp) & (day_opt["strike"] == strike) & (day_opt["time"] > entry_time)].sort_values("time")
            peak = entry_px
            exit_px = entry_px
            for held, (_, b) in enumerate(path.iterrows(), start=1):
                px = float(b["close"])
                peak = max(peak, px)
                exit_px = px
                if px >= entry_px * (1 + TARGET):
                    break  # take the convex pop
                if px <= entry_px * (1 - STOP):
                    break  # hard stop
                if peak >= entry_px * (1 + TRAIL_ARM) and px <= peak * (1 - TRAIL_GIVEBACK):
                    break  # trail
                if held >= TIME_STOP_BARS:
                    break  # fade is fast — don't bleed theta holding to EOD
            return entry_px, exit_px, entry_iv

        r_fade = _run(fade_side)
        if r_fade is None:
            continue
        e, x, entry_iv = r_fade
        # causal IV percentile from prior sessions' entry ATM IVs
        iv_pct = (np.mean([1.0 if entry_iv >= h else 0.0 for h in iv_hist]) if len(iv_hist) >= 50 else 0.5)
        iv_hist.append(entry_iv)
        net = (x - e) * lot - _round_trip_cost(e, x, lot, underlying)
        trades_fade.append({"date": str(d), "net": net, "iv_pct": iv_pct, "ret": (x - e) / e})
        r_mom = _run(mom_side)
        if r_mom is not None:
            em, xm, _ = r_mom
            trades_mom.append({"date": str(d), "net": (xm - em) * lot - _round_trip_cost(em, xm, lot, underlying)})
    return {"fade": trades_fade, "mom": trades_mom, "lot": lot}


def _equity(trades: list[dict], key: str = "net") -> list[float]:
    eq, cum = [], 0.0
    for t in trades:
        cum += t[key]
        eq.append(round(cum, 0))
    return eq


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    nets = np.array([t["net"] for t in trades])
    wins = nets[nets > 0]; losses = nets[nets <= 0]
    eq = np.cumsum(nets)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak).min()
    pf = (wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
    return {
        "n": len(trades), "total": round(float(nets.sum())),
        "win_pct": round(float((nets > 0).mean()) * 100, 1),
        "avg": round(float(nets.mean())), "max_dd": round(float(dd)),
        "profit_factor": round(float(pf), 2),
    }


async def main() -> None:
    out = {}
    for u in ("NIFTY", "BANKNIFTY"):
        df = await _load(u)
        if df.empty:
            continue
        bt = _backtest(df, u)
        fade_all = bt["fade"]
        fade_gated = [t for t in fade_all if t["iv_pct"] <= IV_GATE_PCT]
        out[u] = {
            "dates_fade": [t["date"] for t in fade_all],
            "equity_fade_all": _equity(fade_all),
            "equity_fade_gated": _equity(fade_gated),
            "dates_gated": [t["date"] for t in fade_gated],
            "equity_momentum": _equity(bt["mom"]),
            "stats": {
                "fade_all": _stats(fade_all),
                "fade_gated_lowIV": _stats(fade_gated),
                "momentum_baseline": _stats(bt["mom"]),
            },
            "lot": bt["lot"],
        }
    print("===EQUITY_JSON_START===")
    print(json.dumps(out))
    print("===EQUITY_JSON_END===")
    for u, r in out.items():
        print(f"\n{u} (lot={r['lot']}):")
        for k, s in r["stats"].items():
            print(f"  {k:20} {s}")


if __name__ == "__main__":
    asyncio.run(main())
