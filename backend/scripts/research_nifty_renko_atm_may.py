"""NIFTY May 2026: Renko on 15-min spot, ATM option trade per signal.

Correct methodology (per user feedback):
  1. Build Renko on NIFTY 15-min SPOT closes (not on option premium).
  2. Brick size = ATR(14) on 15-min spot (typically ~50 points for NIFTY).
  3. Each color-change event is ONE signal:
       - red→green  → BUY ATM CE
       - green→red  → BUY ATM PE
  4. Exit on the NEXT opposite color change.
  5. One position at a time; flat between signals.

Result expected: ~9-10 signals across May 2026 (matching the user's chart count
of 5 CE + 4 PE arrows). Each signal trades the May-26 expiry ATM option.

Run:
    docker compose exec backend python -m scripts.research_nifty_renko_atm_may
"""
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_nifty_renko_atm_may"
ATR_PERIOD = 14
EXPIRY = date(2026, 5, 26)  # monthly expiry with widest strike coverage
STRIKE_STEP = 50


@dataclass
class Signal:
    time: str
    direction: str  # "CE" | "PE"
    spot: float
    atm_strike: int


@dataclass
class Trade:
    side: str  # "CE" | "PE"
    strike: int
    entry_time: str
    entry_spot: float
    entry_premium: float
    exit_time: str
    exit_spot: float
    exit_premium: float
    pnl_pct: float
    pnl_points: float
    holding_minutes: int


def true_range(df):
    pc = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)


def atr(df, period=ATR_PERIOD):
    return true_range(df).rolling(period, min_periods=period).mean()


@dataclass
class Brick:
    time: pd.Timestamp
    close: float
    direction: str


def build_renko(closes_df: pd.DataFrame, block: float) -> list[Brick]:
    """Directional Renko on close. 2×block needed for reversal."""
    bricks: list[Brick] = []
    floor = float(closes_df["close"].iloc[0])
    direction = None
    for _, row in closes_df.iterrows():
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


async def load_spot_15min(session, start_date: datetime, end_date: datetime) -> pd.DataFrame:
    """Resample 1-min NIFTY (complete coverage) → 15-min OHLC.
    The native 15-min table is too sparse (only 5 days in our window)."""
    df15 = pd.DataFrame()
    rows1m = (await session.execute(text("""
        SELECT time, open, high, low, close
        FROM underlying_spot_candles
        WHERE underlying='NIFTY' AND interval='1minute'
          AND instrument_key='NSE_INDEX|Nifty 50'
          AND open > 10000 AND high > 10000 AND low > 10000 AND close > 10000
          AND time >= :s AND time < :e
        ORDER BY time
    """), {"s": start_date, "e": end_date})).mappings().all()
    if rows1m:
        df1 = pd.DataFrame([dict(r) for r in rows1m])
        for col in ("open", "high", "low", "close"):
            df1[col] = df1[col].astype(float)
        df1["time"] = pd.to_datetime(df1["time"])
        df1 = df1.set_index("time")
        r15 = pd.DataFrame({
            "open":  df1["open"].resample("15min", label="left", closed="left").first(),
            "high":  df1["high"].resample("15min", label="left", closed="left").max(),
            "low":   df1["low"].resample("15min", label="left", closed="left").min(),
            "close": df1["close"].resample("15min", label="left", closed="left").last(),
        }).dropna().reset_index()
        if df15.empty:
            df15 = r15
        else:
            for col in ("open","high","low","close"):
                df15[col] = df15[col].astype(float)
            df15["time"] = pd.to_datetime(df15["time"])
            # Append any 1-min-resampled bars beyond the last native 15-min bar.
            last_native = df15["time"].max()
            tail = r15[r15["time"] > last_native]
            df15 = pd.concat([df15, tail], ignore_index=True).sort_values("time").reset_index(drop=True)
    return df15


async def load_option_premium(session, strike: int, opt_type: str) -> pd.DataFrame:
    """Prefer 5min option premium for tighter entry/exit fills; fall back to 30min."""
    for iv in ("5minute", "30minute"):
        rows = (await session.execute(text("""
            SELECT time, close
            FROM option_premium_candles
            WHERE underlying='NIFTY' AND expiry=:e AND strike=:s
              AND option_type=:o AND interval=:iv
            ORDER BY time
        """), {"e": EXPIRY, "s": strike, "o": opt_type, "iv": iv})).mappings().all()
        if rows:
            df = pd.DataFrame([dict(r) for r in rows])
            df["close"] = df["close"].astype(float)
            df["time"] = pd.to_datetime(df["time"])
            return df
    return pd.DataFrame()


def lookup_premium_at(df_prem: pd.DataFrame, when: pd.Timestamp) -> float | None:
    """Return the latest option premium close at or before `when`."""
    if df_prem.empty:
        return None
    sub = df_prem[df_prem["time"] <= when]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


async def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    async with AsyncSessionLocal() as session:
        # ── Load NIFTY 15-min spot for May 2026 + buffer for ATR warmup
        spot = await load_spot_15min(session, datetime(2026, 4, 15), datetime(2026, 6, 1))
        for c in ("open","high","low","close"):
            spot[c] = spot[c].astype(float)
        spot["time"] = pd.to_datetime(spot["time"])
        spot = spot.sort_values("time").reset_index(drop=True)
        print(f"15-min NIFTY spot bars: {len(spot)} from {spot['time'].min()} to {spot['time'].max()}")

        # ── ATR(14)
        a = atr(spot)
        first_idx = a.first_valid_index()
        block = float(a.iloc[first_idx])
        print(f"ATR(14) at first valid bar = {block:.2f} → brick size = {round(block):d} points")

        # ── Renko
        trade_df = spot.iloc[first_idx:].reset_index(drop=True)
        bricks = build_renko(trade_df, block)
        print(f"total Renko bricks generated: {len(bricks)}")

        # ── Detect color-change signals across the whole loaded window.
        # Then keep signals whose ENTRY OR HOLDING PERIOD overlaps May 2026.
        # (A late-April down-cross that we ride through May 12 counts as a
        # May trade — that's the big move the chart marks.)
        all_signals: list[Signal] = []
        prev = bricks[0] if bricks else None
        for b in bricks[1:]:
            if b.direction != prev.direction:
                spot_at = float(b.close)
                atm = int(round(spot_at / STRIKE_STEP) * STRIKE_STEP)
                side = "CE" if b.direction == "up" else "PE"
                all_signals.append(Signal(b.time.isoformat(), side, spot_at, atm))
            prev = b
        # Each signal is paired with the NEXT opposite signal for exit time.
        # Keep signals where [entry, exit] overlaps May.
        may_start = pd.Timestamp("2026-05-01", tz="UTC")
        may_end = pd.Timestamp("2026-06-01", tz="UTC")
        signals: list[Signal] = []
        for i, s in enumerate(all_signals):
            entry_t = pd.Timestamp(s.time)
            exit_t = pd.Timestamp(all_signals[i + 1].time) if i + 1 < len(all_signals) else may_end
            # Overlap test: trade is "in May" if its holding window touches May
            if entry_t < may_end and exit_t > may_start:
                signals.append(s)
        print(f"\nall color-change signals in window: {len(all_signals)}")
        print(f"signals whose entry/exit overlaps May 2026: {len(signals)}")
        for s in signals:
            print(f"  {s.time}  {s.direction}  spot={s.spot:.1f}  ATM={s.atm_strike}")

        # ── Build trades: pair each signal with the next opposite signal (exit)
        trades: list[Trade] = []
        prem_cache: dict[tuple[int, str], pd.DataFrame] = {}
        for i, s in enumerate(signals):
            # Exit = next opposite-direction signal, else end of May
            exit_signal = None
            for s2 in signals[i + 1:]:
                if s2.direction != s.direction:
                    exit_signal = s2
                    break
            entry_t = pd.Timestamp(s.time)
            exit_t = pd.Timestamp(exit_signal.time) if exit_signal else pd.Timestamp("2026-05-29 15:30+05:30")
            # Load this strike's premium series
            key = (s.atm_strike, s.direction)
            if key not in prem_cache:
                prem_cache[key] = await load_option_premium(session, s.atm_strike, s.direction)
            prem_df = prem_cache[key]
            if prem_df.empty:
                print(f"  ⚠ no premium data for {s.atm_strike}{s.direction}; skipping")
                continue
            entry_prem = lookup_premium_at(prem_df, entry_t)
            exit_prem = lookup_premium_at(prem_df, exit_t)
            if entry_prem is None or exit_prem is None or entry_prem <= 0:
                print(f"  ⚠ premium lookup failed for {s.atm_strike}{s.direction} entry={entry_prem} exit={exit_prem}")
                continue
            pnl_pct = (exit_prem - entry_prem) / entry_prem * 100.0
            trades.append(Trade(
                side=s.direction,
                strike=s.atm_strike,
                entry_time=s.time,
                entry_spot=s.spot,
                entry_premium=entry_prem,
                exit_time=exit_t.isoformat(),
                exit_spot=float(exit_signal.spot) if exit_signal else float("nan"),
                exit_premium=exit_prem,
                pnl_pct=round(pnl_pct, 2),
                pnl_points=round(exit_prem - entry_prem, 2),
                holding_minutes=int((exit_t - entry_t).total_seconds() // 60),
            ))

        print(f"\n=== TRADES ({len(trades)}) ===")
        for t in trades:
            print(f"  {t.side} {t.strike} | entry {t.entry_time[:16]} prem₹{t.entry_premium:.1f} (spot {t.entry_spot:.0f}) -> "
                  f"exit {t.exit_time[:16]} prem₹{t.exit_premium:.1f} | {t.pnl_pct:+.2f}% ({t.pnl_points:+.1f} pts) | {t.holding_minutes}min")

        # ── Summary
        if trades:
            from statistics import mean, median
            pnls = [t.pnl_pct for t in trades]
            ce = [t for t in trades if t.side == "CE"]
            pe = [t for t in trades if t.side == "PE"]
            summary = {
                "total_trades": len(trades),
                "wins": sum(1 for p in pnls if p > 0),
                "win_rate_pct": round(100*sum(1 for p in pnls if p > 0)/len(pnls), 2),
                "sum_pnl_pct": round(sum(pnls), 2),
                "avg_pnl_pct": round(mean(pnls), 2),
                "median_pnl_pct": round(median(pnls), 2),
                "best": round(max(pnls), 2),
                "worst": round(min(pnls), 2),
                "CE": {"n": len(ce), "avg": round(mean(t.pnl_pct for t in ce), 2) if ce else None, "wins": sum(1 for t in ce if t.pnl_pct > 0)},
                "PE": {"n": len(pe), "avg": round(mean(t.pnl_pct for t in pe), 2) if pe else None, "wins": sum(1 for t in pe if t.pnl_pct > 0)},
                "brick_size_points": round(block, 2),
                "expiry_used": EXPIRY.isoformat(),
            }
            print(f"\n=== SUMMARY ===")
            print(json.dumps(summary, indent=2))
        else:
            summary = {"total_trades": 0}

        # CSV
        with (REPORT_DIR / "trades.csv").open("w", newline="") as f:
            if trades:
                w = csv.DictWriter(f, fieldnames=list(asdict(trades[0]).keys()))
                w.writeheader()
                for t in trades:
                    w.writerow(asdict(t))
        (REPORT_DIR / "signals.json").write_text(json.dumps([asdict(s) for s in signals], indent=2, default=str))
        (REPORT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        print(f"\nwrote: {REPORT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
