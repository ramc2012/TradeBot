"""NIFTY May 2026 — per-option Renko, NO moneyness filter.

User correction: each option's Renko chart is itself the trade signal — strike
moneyness at signal time is NOT a filter. The 23600 PE went from ₹86 → ₹473
(+445%) over May 7-13 even while NIFTY was hundreds of points above 23600,
because its OWN Renko chart fired a clean up-cross when NIFTY started falling.

Setup:
  - Source: option_premium_candles 30-min for NIFTY 2026-05-26 expiry
    (broadest coverage; 5-min is too sparse during the big-move week).
  - Brick: per-option ATR(14) on 30-min OHLC, locked at first valid bar.
  - Rule: long entry on UP brick after DOWN brick; exit on first DOWN brick.
  - Filter: NONE on moneyness. Only filter is "entry happens inside May 2026".
  - Per-strike PnL reported so you can compare to your TradingView charts.

Run:
    docker compose exec backend python -m scripts.research_nifty_per_option_renko_v2
"""
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from statistics import mean, median

import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_nifty_per_option_renko_v2"
EXPIRY = date(2026, 5, 26)
ATR_PERIOD = 14


@dataclass
class Trade:
    side: str
    strike: int
    block_size: float
    entry_time: str
    entry_premium: float
    exit_time: str
    exit_premium: float
    exit_reason: str
    pnl_pct: float
    holding_minutes: int
    bricks_held: int


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


async def list_strikes(session):
    rows = (await session.execute(text("""
        SELECT DISTINCT strike, option_type
        FROM option_premium_candles
        WHERE underlying='NIFTY' AND expiry=:e AND interval='30minute'
        ORDER BY 1, 2
    """), {"e": EXPIRY})).all()
    return [(int(r[0]), r[1]) for r in rows]


async def load_option_30m(session, strike: int, opt: str) -> pd.DataFrame:
    rows = (await session.execute(text("""
        SELECT time, open, high, low, close
        FROM option_premium_candles
        WHERE underlying='NIFTY' AND expiry=:e AND strike=:s
          AND option_type=:o AND interval='30minute'
        ORDER BY time
    """), {"e": EXPIRY, "s": strike, "o": opt})).mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["time"])
    return df


async def main():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    may_start = pd.Timestamp("2026-05-01", tz="UTC")
    may_end = pd.Timestamp("2026-06-01", tz="UTC")

    async with AsyncSessionLocal() as session:
        strikes = await list_strikes(session)
        print(f"NIFTY 2026-05-26 strikes in 30-min table: {len(strikes)}")

        all_trades: list[Trade] = []
        per_strike_report = []

        for strike, opt in strikes:
            df = await load_option_30m(session, strike, opt)
            if len(df) < ATR_PERIOD + 5:
                continue
            a = atr(df)
            first_idx = a.first_valid_index()
            if first_idx is None:
                continue
            block = float(a.iloc[first_idx])
            if block <= 0:
                continue
            trade_df = df.iloc[first_idx:].reset_index(drop=True)
            bricks = build_renko(trade_df, block)
            if len(bricks) < 2:
                continue

            # Walk the bricks: long entry on UP-after-DOWN, exit on next DOWN.
            contract_trades: list[Trade] = []
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
                        # May entries only
                        if entry_t >= may_start and entry_t < may_end:
                            entry_p = float(entry.close)
                            exit_p = float(b.close)
                            pnl_pct = ((exit_p - entry_p) / entry_p * 100.0) if entry_p > 0 else 0.0
                            contract_trades.append(Trade(
                                side=opt, strike=strike, block_size=round(block, 2),
                                entry_time=entry_t.isoformat(), entry_premium=entry_p,
                                exit_time=exit_t.isoformat(), exit_premium=exit_p,
                                exit_reason="down_brick",
                                pnl_pct=round(pnl_pct, 2),
                                holding_minutes=int((exit_t - entry_t).total_seconds() // 60),
                                bricks_held=bricks_in_trade,
                            ))
                        position_open = False
                        entry_brick = None
                        bricks_in_trade = 0
                prev = b
            # If still open at end and entry was in May: mark-to-market.
            if position_open and entry_brick is not None:
                entry_t = pd.Timestamp(entry_brick.time)
                if entry_t >= may_start and entry_t < may_end:
                    last = bricks[-1]
                    entry_p = float(entry_brick.close)
                    exit_p = float(last.close)
                    pnl_pct = ((exit_p - entry_p) / entry_p * 100.0) if entry_p > 0 else 0.0
                    contract_trades.append(Trade(
                        side=opt, strike=strike, block_size=round(block, 2),
                        entry_time=entry_t.isoformat(), entry_premium=entry_p,
                        exit_time=pd.Timestamp(last.time).isoformat(), exit_premium=exit_p,
                        exit_reason="end_of_data",
                        pnl_pct=round(pnl_pct, 2),
                        holding_minutes=int((pd.Timestamp(last.time) - entry_t).total_seconds() // 60),
                        bricks_held=bricks_in_trade,
                    ))

            all_trades.extend(contract_trades)
            if contract_trades:
                per_strike_report.append({
                    "strike": strike, "side": opt, "block": round(block, 2),
                    "n_30m_bars": len(df), "n_bricks": len(bricks),
                    "may_trades": len(contract_trades),
                    "sum_pnl_pct": round(sum(t.pnl_pct for t in contract_trades), 2),
                    "best_trade_pct": round(max(t.pnl_pct for t in contract_trades), 2),
                    "worst_trade_pct": round(min(t.pnl_pct for t in contract_trades), 2),
                })

        # ── Output
        print(f"\n=== PER-STRIKE SUMMARY (May 2026 entries) ===")
        for r in per_strike_report:
            print(f"  {r['strike']:>5}{r['side']}  block₹{r['block']:>7}  bars={r['n_30m_bars']:>3}  bricks={r['n_bricks']:>3}  trades={r['may_trades']}  sum={r['sum_pnl_pct']:>7}%  best={r['best_trade_pct']:>7}%  worst={r['worst_trade_pct']:>7}%")

        print(f"\n=== TOP-10 SINGLE TRADES BY PnL ===")
        for t in sorted(all_trades, key=lambda x: x.pnl_pct, reverse=True)[:10]:
            print(f"  {t.side} {t.strike}  block₹{t.block_size}  entry {t.entry_time[:16]} prem₹{t.entry_premium:.2f}  ->  exit {t.exit_time[:16]} prem₹{t.exit_premium:.2f}  | {t.pnl_pct:+.2f}%  ({t.bricks_held} bricks, {t.holding_minutes}min)")

        print(f"\n=== BOTTOM-10 SINGLE TRADES BY PnL ===")
        for t in sorted(all_trades, key=lambda x: x.pnl_pct)[:10]:
            print(f"  {t.side} {t.strike}  block₹{t.block_size}  entry {t.entry_time[:16]} prem₹{t.entry_premium:.2f}  ->  exit {t.exit_time[:16]} prem₹{t.exit_premium:.2f}  | {t.pnl_pct:+.2f}%  ({t.bricks_held} bricks, {t.holding_minutes}min)")

        if all_trades:
            pnls = [t.pnl_pct for t in all_trades]
            ce = [t for t in all_trades if t.side == "CE"]
            pe = [t for t in all_trades if t.side == "PE"]
            summary = {
                "config": {
                    "expiry": EXPIRY.isoformat(),
                    "interval": "30minute (resampled 15min not viable; 5-min table too sparse)",
                    "block_per_option": "ATR(14) on option's own 30-min OHLC",
                    "rule": "long entry on UP-after-DOWN brick, exit on first DOWN brick",
                    "atm_filter": "NONE (each option's chart is its own signal)",
                },
                "n_trades": len(all_trades),
                "win_rate_pct": round(100 * sum(1 for p in pnls if p > 0) / len(pnls), 2),
                "avg_pnl_pct": round(mean(pnls), 2),
                "median_pnl_pct": round(median(pnls), 2),
                "best_pct": round(max(pnls), 2),
                "worst_pct": round(min(pnls), 2),
                "sum_pnl_pct": round(sum(pnls), 2),
                "CE": {
                    "n": len(ce),
                    "wr": round(100 * sum(1 for t in ce if t.pnl_pct > 0) / len(ce), 2) if ce else None,
                    "avg": round(mean(t.pnl_pct for t in ce), 2) if ce else None,
                    "sum": round(sum(t.pnl_pct for t in ce), 2) if ce else None,
                },
                "PE": {
                    "n": len(pe),
                    "wr": round(100 * sum(1 for t in pe if t.pnl_pct > 0) / len(pe), 2) if pe else None,
                    "avg": round(mean(t.pnl_pct for t in pe), 2) if pe else None,
                    "sum": round(sum(t.pnl_pct for t in pe), 2) if pe else None,
                },
            }
            print(f"\n=== AGGREGATE ===")
            print(json.dumps(summary, indent=2))

        with (REPORT_DIR / "trades.csv").open("w", newline="") as f:
            if all_trades:
                w = csv.DictWriter(f, fieldnames=list(asdict(all_trades[0]).keys()))
                w.writeheader()
                for t in all_trades:
                    w.writerow(asdict(t))
        (REPORT_DIR / "per_strike.json").write_text(json.dumps(per_strike_report, indent=2))
        (REPORT_DIR / "summary.json").write_text(json.dumps(summary if all_trades else {"n_trades": 0}, indent=2))
        print(f"\nwrote: {REPORT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
