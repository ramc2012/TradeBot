"""Renko walk-forward — variant: exit after 1 brick (regardless of direction).

Same rule as research_renko_walkforward.py except:
  - Exit on the FIRST brick that prints after entry, regardless of direction.
  - Compared head-to-head with the original "exit on first DOWN brick" rule.

Run:
    docker compose exec backend python -m scripts.research_renko_walkforward_hold1
"""
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median

import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal

# Reuse helpers from the original script.
from scripts.research_renko_walkforward import (
    ATR_PERIOD,
    MIN_BARS_AFTER_ATR,
    TIMEFRAMES,
    Brick,
    atr,
    build_renko,
    load_bars,
    load_contracts,
    resample_ohlc,
    weekly_folds,
)


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_renko_walkforward_hold1"


@dataclass
class Trade:
    timeframe: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    exit_brick_direction: str  # "up" if continuation, "down" if reversal
    return_pct: float
    block_size: float
    fold_label: str


def simulate_hold1(
    bricks: list[Brick],
    fold_window: tuple[date, date],
    timeframe_label: str,
    meta: dict,
    block: float,
) -> list[Trade]:
    """Long entry on first UP brick after a DOWN brick; EXIT on next brick (any direction)."""
    if len(bricks) < 2:
        return []
    trades: list[Trade] = []
    fold_start, fold_end = fold_window
    fold_label = f"{fold_start.isoformat()}_to_{fold_end.isoformat()}"
    prev = bricks[0]
    i = 1
    while i < len(bricks):
        b = bricks[i]
        # Look for the entry trigger.
        if b.direction == "up" and prev.direction == "down":
            entry = b
            # Exit on the very next brick.
            if i + 1 < len(bricks):
                exit_brick = bricks[i + 1]
                if fold_start <= entry.time.date() < fold_end:
                    entry_price = float(entry.close)
                    exit_price = float(exit_brick.close)
                    ret = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
                    trades.append(
                        Trade(
                            timeframe=timeframe_label,
                            underlying=str(meta["underlying"]),
                            expiry=str(meta["expiry"]),
                            strike=float(meta["strike"]),
                            option_type=str(meta["option_type"]),
                            entry_time=entry.time.isoformat(),
                            entry_price=entry_price,
                            exit_time=exit_brick.time.isoformat(),
                            exit_price=exit_price,
                            exit_brick_direction=exit_brick.direction,
                            return_pct=ret,
                            block_size=block,
                            fold_label=fold_label,
                        )
                    )
                # Skip the exit brick so we don't re-enter on it.
                prev = exit_brick
                i += 2
                continue
            else:
                break
        prev = b
        i += 1
    return trades


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"n_trades": 0}
    rets = [t.return_pct for t in trades]
    wins = [r for r in rets if r > 0]
    return {
        "n_trades": len(trades),
        "n_contracts": len({(t.underlying, t.expiry, t.strike, t.option_type) for t in trades}),
        "win_rate_pct": round(100.0 * len(wins) / len(trades), 2),
        "avg_return_pct": round(mean(rets), 2),
        "median_return_pct": round(median(rets), 2),
        "best_pct": round(max(rets), 2),
        "worst_pct": round(min(rets), 2),
        "exit_brick_up_pct": round(100.0 * sum(1 for t in trades if t.exit_brick_direction == "up") / len(trades), 2),
        "exit_brick_down_pct": round(100.0 * sum(1 for t in trades if t.exit_brick_direction == "down") / len(trades), 2),
    }


async def run_tf(session, db_iv: str, resample: str | None, label: str):
    contracts = await load_contracts(session, db_iv)
    print(f"  [{label}] contracts: {len(contracts)}")
    bounds = (
        await session.execute(
            text("SELECT MIN(time)::date AS s, MAX(time)::date AS e FROM option_premium_candles WHERE interval=:iv"),
            {"iv": db_iv},
        )
    ).mappings().first()
    folds = weekly_folds(bounds["s"], bounds["e"] + timedelta(days=1))
    print(f"  [{label}] folds: {len(folds)}")
    trades: list[Trade] = []
    for ci, c in enumerate(contracts, 1):
        df = await load_bars(session, c, db_iv)
        if df.empty:
            continue
        if resample:
            df = resample_ohlc(df, resample)
        if len(df) < ATR_PERIOD + MIN_BARS_AFTER_ATR:
            continue
        a = atr(df)
        first_idx = a.first_valid_index()
        if first_idx is None:
            continue
        block = float(a.iloc[first_idx])
        if block <= 0:
            continue
        df_trade = df.iloc[first_idx:].reset_index(drop=True)
        bricks = build_renko(df_trade, block)
        if len(bricks) < 2:
            continue
        meta = {"underlying": c[0], "expiry": c[1], "strike": c[2], "option_type": c[3]}
        for f in folds:
            trades.extend(simulate_hold1(bricks, f, label, meta, block))
        if ci % 500 == 0:
            print(f"  [{label}] processed {ci}/{len(contracts)}; trades so far: {len(trades)}")
    # Per-fold rows
    fold_rows = []
    for f in folds:
        flabel = f"{f[0].isoformat()}_to_{f[1].isoformat()}"
        ft = [t for t in trades if t.fold_label == flabel]
        s = summarize(ft)
        if s["n_trades"]:
            ce = summarize([t for t in ft if t.option_type == "CE"])
            pe = summarize([t for t in ft if t.option_type == "PE"])
            fold_rows.append(
                {
                    "fold": flabel,
                    "n": s["n_trades"],
                    "wr": s["win_rate_pct"],
                    "avg": s["avg_return_pct"],
                    "med": s["median_return_pct"],
                    "best": s["best_pct"],
                    "worst": s["worst_pct"],
                    "CE_n": ce.get("n_trades", 0),
                    "CE_wr": ce.get("win_rate_pct"),
                    "CE_avg": ce.get("avg_return_pct"),
                    "PE_n": pe.get("n_trades", 0),
                    "PE_wr": pe.get("win_rate_pct"),
                    "PE_avg": pe.get("avg_return_pct"),
                }
            )
    return trades, fold_rows


async def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "config": {
            "block_size": "1 × ATR(14)",
            "rule_entry": "first UP brick after a DOWN brick → BUY at brick-printing bar's close",
            "rule_exit": "the very next brick (any direction) → SELL at that brick's close",
            "walk_forward": "weekly OOS folds, entry must be inside fold",
            "long_only": True,
        }
    }
    async with AsyncSessionLocal() as session:
        for db_iv, resample, label in TIMEFRAMES:
            print(f"\n=== {label} renko (HOLD=1) ===")
            trades, folds = await run_tf(session, db_iv, resample, label)
            with (REPORT_DIR / f"trades_{label}.csv").open("w", newline="") as f:
                if trades:
                    w = csv.DictWriter(f, fieldnames=list(asdict(trades[0]).keys()))
                    w.writeheader()
                    for t in trades:
                        w.writerow(asdict(t))
            with (REPORT_DIR / f"folds_{label}.csv").open("w", newline="") as f:
                if folds:
                    w = csv.DictWriter(f, fieldnames=list(folds[0].keys()))
                    w.writeheader()
                    for r in folds:
                        w.writerow(r)
            out[label] = {
                "overall": summarize(trades),
                "CE": summarize([t for t in trades if t.option_type == "CE"]),
                "PE": summarize([t for t in trades if t.option_type == "PE"]),
                "folds": folds,
            }
    (REPORT_DIR / "summary.json").write_text(json.dumps(out, indent=2, default=str))
    for tf in ("15min", "30min"):
        if tf in out:
            print(f"\n=== {tf} OVERALL (HOLD=1) ===")
            print(json.dumps(out[tf]["overall"], indent=2))
            print(f"  CE: {json.dumps(out[tf]['CE'])}")
            print(f"  PE: {json.dumps(out[tf]['PE'])}")
            print(f"--- {tf} folds ---")
            for fr in out[tf]["folds"]:
                print(
                    f"  {fr['fold']}  n={fr['n']:4} wr={fr['wr']:5}% avg={fr['avg']:6}% med={fr['med']:6}%  CE n/wr/avg={fr['CE_n']}/{fr['CE_wr']}/{fr['CE_avg']}  PE n/wr/avg={fr['PE_n']}/{fr['PE_wr']}/{fr['PE_avg']}"
                )
    print(f"\nwrote: {REPORT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
