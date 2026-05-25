"""NIFTY May 2026 — per-option 15-min Renko (block = option's own ATR(14)).

Per user feedback:
  - Compute ATR per OPTION instrument on 15-min OHLC of the option premium.
  - Use that ATR as the Renko brick size for THAT option's Renko chart.
  - Filter signals to ATM-near contracts only (|strike - spot| ≤ ATM_TOL_POINTS).
  - May 2026 only.

Setup:
  - Source: option_premium_candles 5-min for NIFTY 2026-05-26 expiry, resampled
    to 15-min OHLC per (strike, option_type).
  - Brick: ATR(14) on each option's 15-min series, locked in at first valid bar.
  - Rule: long entry on first UP brick after a DOWN brick; exit on first DOWN
    brick after entry. (Hold-to-down — the variant with the best gross edge
    in earlier walk-forward tests.)
  - ATM filter: only count entries where |strike - NIFTY spot| ≤ ATM_TOL_POINTS
    at the entry timestamp.

Run:
    docker compose exec backend python -m scripts.research_nifty_atm_renko_per_option
"""
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median

import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_nifty_atm_renko_per_option"
EXPIRY = date(2026, 5, 26)
ATR_PERIOD = 14
ATM_TOL_POINTS = 100  # strike must be within ±100 of spot at signal time


@dataclass
class Trade:
    side: str
    strike: int
    block_size: float
    entry_time: str
    entry_spot: float
    entry_premium: float
    exit_time: str
    exit_spot: float
    exit_premium: float
    pnl_pct: float
    pnl_points: float
    moneyness_at_entry: int  # |strike - spot|
    holding_minutes: int
    bricks_held: int


# ── Renko + ATR helpers ─────────────────────────────────────────────────────
def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    return true_range(df).rolling(period, min_periods=period).mean()


@dataclass
class Brick:
    time: pd.Timestamp
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
async def list_strikes(session) -> list[tuple[int, str]]:
    rows = (await session.execute(text("""
        SELECT DISTINCT strike, option_type
        FROM option_premium_candles
        WHERE underlying='NIFTY' AND expiry=:e AND interval='5minute'
        ORDER BY 1, 2
    """), {"e": EXPIRY})).all()
    return [(int(r[0]), r[1]) for r in rows]


async def load_option_5m(session, strike: int, opt: str) -> pd.DataFrame:
    rows = (await session.execute(text("""
        SELECT time, open, high, low, close
        FROM option_premium_candles
        WHERE underlying='NIFTY' AND expiry=:e AND strike=:s
          AND option_type=:o AND interval='5minute'
        ORDER BY time
    """), {"e": EXPIRY, "s": strike, "o": opt})).mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["time"])
    return df


def resample_15min(df_5m: pd.DataFrame) -> pd.DataFrame:
    if df_5m.empty:
        return df_5m
    d = df_5m.set_index("time").sort_index()
    r = pd.DataFrame({
        "open":  d["open"].resample("15min", label="left", closed="left").first(),
        "high":  d["high"].resample("15min", label="left", closed="left").max(),
        "low":   d["low"].resample("15min", label="left", closed="left").min(),
        "close": d["close"].resample("15min", label="left", closed="left").last(),
    }).dropna().reset_index()
    return r


async def load_nifty_spot(session) -> pd.DataFrame:
    """1-min NIFTY 50, filtered for sane prices."""
    rows = (await session.execute(text("""
        SELECT time, close
        FROM underlying_spot_candles
        WHERE underlying='NIFTY' AND interval='1minute'
          AND instrument_key='NSE_INDEX|Nifty 50'
          AND close > 10000
        ORDER BY time
    """))).mappings().all()
    df = pd.DataFrame([dict(r) for r in rows])
    df["close"] = df["close"].astype(float)
    df["time"] = pd.to_datetime(df["time"])
    return df


def spot_at(df_spot: pd.DataFrame, when: pd.Timestamp) -> float | None:
    sub = df_spot[df_spot["time"] <= when]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


# ── Main ────────────────────────────────────────────────────────────────────
async def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    may_start = pd.Timestamp("2026-05-01", tz="UTC")
    may_end = pd.Timestamp("2026-06-01", tz="UTC")

    async with AsyncSessionLocal() as session:
        spot_df = await load_nifty_spot(session)
        print(f"NIFTY 1-min spot rows (clean): {len(spot_df)}  range {spot_df['time'].min()} → {spot_df['time'].max()}")
        strikes = await list_strikes(session)
        print(f"NIFTY {EXPIRY} 5-min strikes: {strikes}")

        all_trades: list[Trade] = []
        per_contract_stats: list[dict] = []

        for strike, opt in strikes:
            df_5m = await load_option_5m(session, strike, opt)
            df_15m = resample_15min(df_5m)
            if len(df_15m) < ATR_PERIOD + 5:
                continue
            a = atr(df_15m)
            first_idx = a.first_valid_index()
            if first_idx is None:
                continue
            block = float(a.iloc[first_idx])
            if block <= 0:
                continue
            trade_df = df_15m.iloc[first_idx:].reset_index(drop=True)
            bricks = build_renko(trade_df, block)
            if len(bricks) < 2:
                continue

            # Walk bricks: long entry on UP-after-DOWN, exit on DOWN.
            contract_trades = 0
            position_open = False
            entry_brick: Brick | None = None
            bricks_in_trade = 0
            prev = bricks[0]
            for b in bricks[1:]:
                if not position_open:
                    if b.direction == "up" and prev.direction == "down":
                        entry_brick = b
                        position_open = True
                        bricks_in_trade = 1
                else:
                    bricks_in_trade += 1
                    if b.direction == "down":
                        entry = entry_brick
                        entry_t = pd.Timestamp(entry.time)
                        exit_t = pd.Timestamp(b.time)
                        # May window filter (entry inside May)
                        if entry_t >= may_start and entry_t < may_end:
                            entry_spot = spot_at(spot_df, entry_t)
                            exit_spot = spot_at(spot_df, exit_t)
                            if entry_spot is None or exit_spot is None:
                                position_open = False; entry_brick = None; bricks_in_trade = 0
                                prev = b; continue
                            moneyness = abs(strike - entry_spot)
                            if moneyness <= ATM_TOL_POINTS:
                                entry_p = float(entry.close)
                                exit_p = float(b.close)
                                pnl_pct = ((exit_p - entry_p) / entry_p * 100.0) if entry_p > 0 else 0.0
                                all_trades.append(Trade(
                                    side=opt, strike=strike, block_size=round(block, 2),
                                    entry_time=entry_t.isoformat(),
                                    entry_spot=round(entry_spot, 2),
                                    entry_premium=entry_p,
                                    exit_time=exit_t.isoformat(),
                                    exit_spot=round(exit_spot, 2),
                                    exit_premium=exit_p,
                                    pnl_pct=round(pnl_pct, 2),
                                    pnl_points=round(exit_p - entry_p, 2),
                                    moneyness_at_entry=int(moneyness),
                                    holding_minutes=int((exit_t - entry_t).total_seconds() // 60),
                                    bricks_held=bricks_in_trade,
                                ))
                                contract_trades += 1
                        position_open = False
                        entry_brick = None
                        bricks_in_trade = 0
                prev = b
            per_contract_stats.append({
                "strike": strike, "side": opt, "block": round(block, 2),
                "n_15m_bars": len(df_15m), "n_bricks": len(bricks),
                "may_atm_trades": contract_trades,
            })

        # ── Reporting
        print("\n=== PER-CONTRACT STATS ===")
        for s in per_contract_stats:
            print(f"  {s['strike']:>5}{s['side']}  block₹{s['block']:>6}  bars={s['n_15m_bars']:>3}  bricks={s['n_bricks']:>3}  May ATM trades={s['may_atm_trades']}")
        print(f"\n=== MAY ATM TRADES ({len(all_trades)}) ===")
        for t in all_trades:
            print(f"  {t.side} {t.strike}  entry {t.entry_time[:16]} spot={t.entry_spot} prem₹{t.entry_premium:.1f}  ->  "
                  f"exit {t.exit_time[:16]} spot={t.exit_spot} prem₹{t.exit_premium:.1f}  |  "
                  f"{t.pnl_pct:+.2f}%  block=₹{t.block_size}  hold={t.holding_minutes}min ({t.bricks_held} bricks)  moneyness={t.moneyness_at_entry}")

        if all_trades:
            pnls = [t.pnl_pct for t in all_trades]
            ce = [t for t in all_trades if t.side == "CE"]
            pe = [t for t in all_trades if t.side == "PE"]
            summary = {
                "config": {
                    "expiry": EXPIRY.isoformat(),
                    "block_per_option": "ATR(14) on option's own 15-min OHLC, locked at first valid bar",
                    "atm_filter_points": ATM_TOL_POINTS,
                    "rule": "long entry on UP-after-DOWN brick, exit on first DOWN brick",
                    "month": "2026-05",
                },
                "n_trades": len(all_trades),
                "wins": sum(1 for p in pnls if p > 0),
                "win_rate_pct": round(100*sum(1 for p in pnls if p > 0)/len(pnls), 2),
                "avg_pnl_pct": round(mean(pnls), 2),
                "median_pnl_pct": round(median(pnls), 2),
                "best_pct": round(max(pnls), 2),
                "worst_pct": round(min(pnls), 2),
                "sum_pnl_pct": round(sum(pnls), 2),
                "median_hold_minutes": int(median([t.holding_minutes for t in all_trades])),
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
            if all_trades:
                w = csv.DictWriter(f, fieldnames=list(asdict(all_trades[0]).keys()))
                w.writeheader()
                for t in all_trades:
                    w.writerow(asdict(t))
        (REPORT_DIR / "per_contract.json").write_text(json.dumps(per_contract_stats, indent=2, default=str))
        (REPORT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        print(f"\nwrote: {REPORT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
