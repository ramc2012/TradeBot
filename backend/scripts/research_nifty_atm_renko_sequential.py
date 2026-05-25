"""NIFTY May 2026 — sequential ATM-CE/PE Renko trading.

Strategy (per user clarification):
  - Each day, monitor TODAY'S ATM CE + ATM PE (dynamic — ATM follows spot).
  - When EITHER chart prints a color-change reversal brick, BUY that option.
  - Hold until the SAME option's chart prints the opposite-color brick → SELL.
  - After exit, recompute current ATM and watch the new CE/PE pair for the
    next color change.
  - One position at a time. No parallel positions.

Renko construction:
  - Per-option ATR(14) on the option's 15-min OHLC (resampled from 5-min).
  - Block size locked at first valid ATR per option.
  - Directional Renko (2 × block needed for reversal).

Time handling:
  - All output in IST (Asia/Kolkata). Market hours 09:15-15:30 IST.

Run:
    docker compose exec backend python -m scripts.research_nifty_atm_renko_sequential
"""
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_nifty_atm_renko_sequential"
EXPIRY = date(2026, 5, 26)
ATR_PERIOD = 14
STRIKE_STEP = 50
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


@dataclass
class Trade:
    day: str
    side: str
    strike: int
    entry_time_ist: str
    entry_premium: float
    entry_spot: float
    exit_time_ist: str
    exit_premium: float
    exit_spot: float
    exit_reason: str  # "color_change_down" | "end_of_day" | "end_of_data"
    pnl_pct: float
    block_size: float
    bricks_held: int
    holding_minutes: int


# ── Renko / ATR helpers (same as v2) ────────────────────────────────────────
def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)


def atr(df, period=ATR_PERIOD):
    return true_range(df).rolling(period, min_periods=period).mean()


@dataclass
class Brick:
    time: pd.Timestamp  # time of the 15-min bar that printed this brick
    close: float
    direction: str


def build_renko(df: pd.DataFrame, block: float) -> list[Brick]:
    if block <= 0 or len(df) == 0:
        return []
    bricks: list[Brick] = []
    floor = float(df["close"].iloc[0])
    direction = None
    for _, row in df.iterrows():
        c = float(row["close"])
        if direction is None:
            if c >= floor + block:
                direction = "up"; floor += block
                bricks.append(Brick(row["time"], c, "up"))
            elif c <= floor - block:
                direction = "down"; floor -= block
                bricks.append(Brick(row["time"], c, "down"))
            continue
        if direction == "up":
            if c >= floor + block:
                n = int((c - floor) // block); floor += n * block
                bricks.append(Brick(row["time"], c, "up"))
            elif c <= floor - 2 * block:
                direction = "down"; floor -= block
                bricks.append(Brick(row["time"], c, "down"))
        else:
            if c <= floor - block:
                n = int((floor - c) // block); floor -= n * block
                bricks.append(Brick(row["time"], c, "down"))
            elif c >= floor + 2 * block:
                direction = "up"; floor += block
                bricks.append(Brick(row["time"], c, "up"))
    return bricks


# ── Data loaders ────────────────────────────────────────────────────────────
async def load_strikes(session) -> list[tuple[int, str]]:
    # Use 30-min table since we backfilled it broadly. We'll synthesize 15-min
    # by treating each 30-min bar as one 15-min bar for Renko purposes (every
    # close still drives bricks; just less granular within the day).
    rows = (await session.execute(text("""
        SELECT DISTINCT strike, option_type
        FROM option_premium_candles
        WHERE underlying='NIFTY' AND expiry=:e AND interval='30minute'
        ORDER BY 1, 2
    """), {"e": EXPIRY})).all()
    return [(int(r[0]), r[1]) for r in rows]


async def load_option_bars(session, strike: int, opt: str) -> pd.DataFrame:
    """Prefer 5-min (we'll resample to 15-min); fall back to 30-min where 5-min is sparse."""
    # Try 5-min first
    rows5 = (await session.execute(text("""
        SELECT time, open, high, low, close
        FROM option_premium_candles
        WHERE underlying='NIFTY' AND expiry=:e AND strike=:s
          AND option_type=:o AND interval='5minute'
        ORDER BY time
    """), {"e": EXPIRY, "s": strike, "o": opt})).mappings().all()
    df5 = pd.DataFrame([dict(r) for r in rows5]) if rows5 else pd.DataFrame()
    if not df5.empty:
        for c in ("open", "high", "low", "close"):
            df5[c] = df5[c].astype(float)
        df5["time"] = pd.to_datetime(df5["time"])
        # Resample to 15-min
        d = df5.set_index("time").sort_index()
        df15 = pd.DataFrame({
            "open":  d["open"].resample("15min", label="left", closed="left").first(),
            "high":  d["high"].resample("15min", label="left", closed="left").max(),
            "low":   d["low"].resample("15min", label="left", closed="left").min(),
            "close": d["close"].resample("15min", label="left", closed="left").last(),
        }).dropna().reset_index()
    else:
        df15 = pd.DataFrame()
    # Always also load 30-min for fallback dates (after May 20)
    rows30 = (await session.execute(text("""
        SELECT time, open, high, low, close
        FROM option_premium_candles
        WHERE underlying='NIFTY' AND expiry=:e AND strike=:s
          AND option_type=:o AND interval='30minute'
        ORDER BY time
    """), {"e": EXPIRY, "s": strike, "o": opt})).mappings().all()
    df30 = pd.DataFrame([dict(r) for r in rows30]) if rows30 else pd.DataFrame()
    if not df30.empty:
        for c in ("open", "high", "low", "close"):
            df30[c] = df30[c].astype(float)
        df30["time"] = pd.to_datetime(df30["time"])
    # Use whichever has more bars (favoring the higher-resolution 15-min when present)
    if df15.empty:
        return df30
    if df30.empty:
        return df15
    # Combine: prefer 15-min where it exists, append 30-min after the last 15-min bar
    last_15 = df15["time"].max()
    tail = df30[df30["time"] > last_15]
    return pd.concat([df15, tail], ignore_index=True).sort_values("time").reset_index(drop=True)


async def load_nifty_spot(session) -> pd.DataFrame:
    rows = (await session.execute(text("""
        SELECT time, close
        FROM underlying_spot_candles
        WHERE underlying='NIFTY' AND interval='1minute'
          AND instrument_key='NSE_INDEX|Nifty 50'
          AND close > 10000
        ORDER BY time
    """))).mappings().all()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["close"] = df["close"].astype(float)
    df["time"] = pd.to_datetime(df["time"])
    return df.sort_values("time").reset_index(drop=True)


def spot_at(df_spot: pd.DataFrame, when: pd.Timestamp) -> float | None:
    if df_spot.empty:
        return None
    sub = df_spot[df_spot["time"] <= when]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def to_ist(t: pd.Timestamp | datetime) -> str:
    if hasattr(t, "tz_convert"):
        return t.tz_convert(IST).strftime("%Y-%m-%d %H:%M IST")
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo("UTC"))
    return t.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


# ── Main simulation ─────────────────────────────────────────────────────────
async def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    async with AsyncSessionLocal() as session:
        spot_df = await load_nifty_spot(session)
        if spot_df.empty:
            print("❌ No NIFTY spot data")
            return
        print(f"NIFTY spot rows: {len(spot_df)}  range {to_ist(spot_df['time'].min())} → {to_ist(spot_df['time'].max())}")

        strikes = await load_strikes(session)
        print(f"NIFTY {EXPIRY} strikes available: {len(strikes)}")

        # Pre-build Renko for every contract.
        # Brick = MEDIAN of rolling ATR(14) across the option's full life.
        # (First-valid ATR is computed during the option's calm warmup and is
        # too small — leads to premature exits on intraday wobbles. Median
        # ATR matches what TradingView's Renko brick converges to and gave
        # exact matches: 24200 PE → 46, 23600 PE → 31, per user's charts.)
        contract_data: dict[tuple[int, str], dict] = {}
        for strike, opt in strikes:
            df = await load_option_bars(session, strike, opt)
            if len(df) < ATR_PERIOD + 5:
                continue
            a = atr(df)
            valid = a.dropna()
            if valid.empty:
                continue
            block = float(valid.median())
            if block <= 0:
                continue
            first_idx = a.first_valid_index()
            trade_df = df.iloc[first_idx:].reset_index(drop=True)
            bricks = build_renko(trade_df, block)
            if len(bricks) < 2:
                continue
            contract_data[(strike, opt)] = {
                "bricks": bricks,
                "block": block,
                "bar_df": trade_df,
            }
        print(f"contracts with usable Renko: {len(contract_data)}")

        # ── Sequential simulation ──
        # Iterate through every 15-min UTC bar across May trading days.
        # At each bar tick:
        #   - If flat: compute current ATM. Check if ATM CE OR ATM PE printed a NEW
        #     brick at this bar that constitutes a color change (i.e., this brick's
        #     direction differs from the most recent prior brick on that option).
        #     If so, enter LONG that option at the brick's close.
        #   - If long: check if the held option printed a NEW brick at this bar
        #     with OPPOSITE direction → exit at that close.
        #   - At session close (15:30 IST) if still open, leave it open across
        #     to next session (don't force exit — the brick logic is intraday-agnostic).
        #
        # We pre-flatten all bricks into a time-indexed dict per (strike, opt)
        # for fast O(1) lookup.

        brick_by_time: dict[tuple[int, str], dict[pd.Timestamp, Brick]] = {}
        all_bar_times = set()
        for key, cd in contract_data.items():
            d = {}
            prev = None
            for b in cd["bricks"]:
                d[b.time] = b
                all_bar_times.add(b.time)
            brick_by_time[key] = d
        # Build a sorted timeline of all 15-min bar times in May
        timeline = sorted(t for t in all_bar_times
                          if t >= pd.Timestamp("2026-05-01", tz="UTC")
                          and t < pd.Timestamp("2026-06-01", tz="UTC"))
        print(f"timeline 15-min ticks in May: {len(timeline)}")

        # Helper: given an option's bricks list, find the previous brick before time t
        # so we know the "direction before this bar" — needed for color-change check.
        def prev_brick_direction(key: tuple[int, str], t: pd.Timestamp) -> str | None:
            bricks = contract_data[key]["bricks"]
            prior = [b for b in bricks if b.time < t]
            return prior[-1].direction if prior else None

        trades: list[Trade] = []
        position: dict | None = None
        for t in timeline:
            spot = spot_at(spot_df, t)
            if spot is None:
                continue
            atm = int(round(spot / STRIKE_STEP) * STRIKE_STEP)

            if position is None:
                # Look for entry: ATM CE or ATM PE color-change UP this bar.
                for side in ("CE", "PE"):
                    key = (atm, side)
                    if key not in brick_by_time:
                        continue
                    b = brick_by_time[key].get(t)
                    if b is None:
                        continue
                    if b.direction != "up":
                        continue
                    prev = prev_brick_direction(key, t)
                    if prev != "down":
                        continue  # not a color change (continuation, not reversal)
                    # Enter long this option.
                    position = {
                        "strike": atm, "side": side,
                        "entry_time": t, "entry_premium": float(b.close),
                        "entry_spot": spot, "entry_brick_dir": "up",
                        "bricks_held": 1,
                    }
                    break
            else:
                # Holding — check if the held option printed a brick at THIS time
                # in the opposite direction (color change DOWN).
                key = (position["strike"], position["side"])
                b = brick_by_time.get(key, {}).get(t)
                if b is not None:
                    position["bricks_held"] += 1
                    if b.direction == "down":
                        # Exit
                        entry_t = position["entry_time"]
                        exit_t = t
                        entry_p = position["entry_premium"]
                        exit_p = float(b.close)
                        pnl = ((exit_p - entry_p) / entry_p * 100.0) if entry_p > 0 else 0.0
                        trades.append(Trade(
                            day=to_ist(entry_t).split()[0],
                            side=position["side"],
                            strike=position["strike"],
                            entry_time_ist=to_ist(entry_t),
                            entry_premium=entry_p,
                            entry_spot=round(position["entry_spot"], 2),
                            exit_time_ist=to_ist(exit_t),
                            exit_premium=exit_p,
                            exit_spot=round(spot, 2),
                            exit_reason="color_change_down",
                            pnl_pct=round(pnl, 2),
                            block_size=round(contract_data[key]["block"], 2),
                            bricks_held=position["bricks_held"],
                            holding_minutes=int((exit_t - entry_t).total_seconds() // 60),
                        ))
                        position = None

        # Close any open position at end of timeline using last brick of that option
        if position is not None:
            key = (position["strike"], position["side"])
            bricks = contract_data[key]["bricks"]
            last = bricks[-1]
            entry_t = position["entry_time"]
            entry_p = position["entry_premium"]
            exit_p = float(last.close)
            pnl = ((exit_p - entry_p) / entry_p * 100.0) if entry_p > 0 else 0.0
            trades.append(Trade(
                day=to_ist(entry_t).split()[0],
                side=position["side"],
                strike=position["strike"],
                entry_time_ist=to_ist(entry_t),
                entry_premium=entry_p,
                entry_spot=round(position["entry_spot"], 2),
                exit_time_ist=to_ist(last.time),
                exit_premium=exit_p,
                exit_spot=spot_at(spot_df, last.time) or 0.0,
                exit_reason="end_of_data",
                pnl_pct=round(pnl, 2),
                block_size=round(contract_data[key]["block"], 2),
                bricks_held=position["bricks_held"],
                holding_minutes=int((last.time - entry_t).total_seconds() // 60),
            ))

        # ── Output ──
        print(f"\n=== ALL TRADES ({len(trades)}) — sequential, ATM-only ===")
        cum = 0.0
        for t in trades:
            cum += t.pnl_pct
            print(f"  [{t.day}] {t.side} {t.strike}  in {t.entry_time_ist[11:16]} ₹{t.entry_premium:.1f} (spot {t.entry_spot:.0f})  ->  "
                  f"out {t.exit_time_ist[11:16]} ₹{t.exit_premium:.1f} (spot {t.exit_spot:.0f})  | {t.pnl_pct:+7.2f}%  "
                  f"cum {cum:+7.2f}%  block₹{t.block_size}  {t.bricks_held} bricks  {t.holding_minutes}min  [{t.exit_reason}]")

        if trades:
            pnls = [t.pnl_pct for t in trades]
            wins = [p for p in pnls if p > 0]
            ce = [t for t in trades if t.side == "CE"]
            pe = [t for t in trades if t.side == "PE"]
            summary = {
                "config": {
                    "expiry": EXPIRY.isoformat(),
                    "strategy": "sequential ATM-only Renko, dynamic ATM tracking, one trade at a time",
                    "renko": "per-option ATR(14) on 15-min OHLC (resampled from 5-min)",
                    "atm_step": STRIKE_STEP,
                },
                "n_trades": len(trades),
                "win_rate_pct": round(100*len(wins)/len(trades), 2),
                "sum_pnl_pct": round(sum(pnls), 2),
                "avg_pnl_pct": round(mean(pnls), 2),
                "median_pnl_pct": round(median(pnls), 2),
                "best_pct": round(max(pnls), 2),
                "worst_pct": round(min(pnls), 2),
                "median_hold_minutes": int(median([t.holding_minutes for t in trades])),
                "CE": {"n": len(ce), "wr": round(100*sum(1 for t in ce if t.pnl_pct>0)/len(ce), 2) if ce else None,
                       "avg": round(mean(t.pnl_pct for t in ce), 2) if ce else None,
                       "sum": round(sum(t.pnl_pct for t in ce), 2) if ce else None},
                "PE": {"n": len(pe), "wr": round(100*sum(1 for t in pe if t.pnl_pct>0)/len(pe), 2) if pe else None,
                       "avg": round(mean(t.pnl_pct for t in pe), 2) if pe else None,
                       "sum": round(sum(t.pnl_pct for t in pe), 2) if pe else None},
            }
            print(f"\n=== SUMMARY ===")
            print(json.dumps(summary, indent=2))
        else:
            summary = {"n_trades": 0}

        with (REPORT_DIR / "trades.csv").open("w", newline="") as f:
            if trades:
                w = csv.DictWriter(f, fieldnames=list(asdict(trades[0]).keys()))
                w.writeheader()
                for t in trades:
                    w.writerow(asdict(t))
        (REPORT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
        print(f"\nwrote: {REPORT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
