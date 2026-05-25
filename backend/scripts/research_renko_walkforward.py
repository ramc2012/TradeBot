"""Walk-forward backtest: Renko (block = 1×ATR) on option premium.

Rules
-----
For each contract:
  1. Resample raw bars to the target timeframe (15min or 30min).
  2. Compute ATR(14) on the resampled OHLC. Use first valid ATR value as the
     fixed Renko block size for that contract.
  3. Build directional Renko (TradingView-style):
        starting up:   new UP if close ≥ floor + block; new DOWN if close ≤ floor − 2·block (reversal)
        starting down: new DOWN if close ≤ floor − block; new UP if close ≥ floor + 2·block
  4. Trading rule:
        LONG entry on the bar that prints the FIRST UP brick after a DOWN brick.
        LONG exit  on the bar that prints the FIRST DOWN brick after entry.
        Entry / exit fill price = close of the bar that printed the brick.
        End-of-data fallback: exit at last close.
  5. Walk-forward folds:
        Each OOS fold is a calendar week. We only count trades whose ENTRY
        falls inside the OOS week. Warmup of ≥ 14 resampled bars (ATR
        warmup) must precede the OOS window; this is automatic because
        ATR is built from the contract's full prior history at that point.

Outputs
-------
  reports/research_renko_walkforward/
    summary.json             — aggregate + per-timeframe + per-fold
    trades_15min.csv         — every OOS trade (15-min Renko)
    trades_30min.csv         — every OOS trade (30-min Renko)
    folds_15min.csv          — one row per (fold, side) with summary stats
    folds_30min.csv

Run:
    docker compose exec backend python -m scripts.research_renko_walkforward
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


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_renko_walkforward"
ATR_PERIOD = 14
MIN_BARS_AFTER_ATR = 5
# (db_interval_label, resample_freq_pandas, output_label)
TIMEFRAMES = [
    ("5minute", "15min", "15min"),
    ("30minute", None, "30min"),  # native, no resample
]


# ── Trade record ────────────────────────────────────────────────────────────
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
    exit_reason: str  # "down_brick" | "end_of_data"
    return_pct: float
    block_size: float
    bricks_held: int
    fold_label: str


# ── ATR ─────────────────────────────────────────────────────────────────────
def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    return true_range(df).rolling(window=period, min_periods=period).mean()


# ── Renko ───────────────────────────────────────────────────────────────────
@dataclass
class Brick:
    time: pd.Timestamp
    close: float
    direction: str  # "up" | "down"
    floor: float  # the brick "anchor" after this brick prints


def build_renko(df: pd.DataFrame, block: float) -> list[Brick]:
    """Directional Renko from close series; emits one Brick per bar that
    prints a NEW brick. Reversal needs 2×block move."""
    if block <= 0 or len(df) == 0:
        return []
    bricks: list[Brick] = []
    floor = float(df["close"].iloc[0])
    direction: str | None = None
    for _, row in df.iterrows():
        close = float(row["close"])
        if direction is None:
            # Seed the first brick on the first sufficient move.
            if close >= floor + block:
                direction = "up"
                floor = floor + block
                bricks.append(Brick(row["time"], close, "up", floor))
            elif close <= floor - block:
                direction = "down"
                floor = floor - block
                bricks.append(Brick(row["time"], close, "down", floor))
            continue
        if direction == "up":
            if close >= floor + block:
                # New up brick(s). Snap floor up to the highest brick boundary.
                n = int((close - floor) // block)
                floor = floor + n * block
                bricks.append(Brick(row["time"], close, "up", floor))
            elif close <= floor - 2 * block:
                # Reversal: print one down brick at floor - block.
                direction = "down"
                floor = floor - block
                bricks.append(Brick(row["time"], close, "down", floor))
        else:  # direction == "down"
            if close <= floor - block:
                n = int((floor - close) // block)
                floor = floor - n * block
                bricks.append(Brick(row["time"], close, "down", floor))
            elif close >= floor + 2 * block:
                direction = "up"
                floor = floor + block
                bricks.append(Brick(row["time"], close, "up", floor))
    return bricks


# ── Trading rule ────────────────────────────────────────────────────────────
def simulate(
    bricks: list[Brick],
    fold_window: tuple[date, date],
    timeframe_label: str,
    meta: dict,
    block: float,
) -> list[Trade]:
    """Apply the Renko long-only rule and return trades whose ENTRY is in fold_window."""
    if len(bricks) < 2:
        return []
    trades: list[Trade] = []
    position_open = False
    entry_brick: Brick | None = None
    bricks_in_trade = 0
    prev = bricks[0]
    fold_start, fold_end = fold_window
    fold_label = f"{fold_start.isoformat()}_to_{fold_end.isoformat()}"

    for b in bricks[1:]:
        # Long entry: first UP brick after a DOWN brick.
        if not position_open:
            if b.direction == "up" and prev.direction == "down":
                entry_brick = b
                position_open = True
                bricks_in_trade = 1
        else:
            bricks_in_trade += 1
            if b.direction == "down":
                entry = entry_brick
                if entry is None:
                    position_open = False
                    prev = b
                    continue
                # Only count if entry is within OOS fold window.
                if fold_start <= entry.time.date() < fold_end:
                    entry_price = float(entry.close)
                    exit_price = float(b.close)
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
                            exit_time=b.time.isoformat(),
                            exit_price=exit_price,
                            exit_reason="down_brick",
                            return_pct=ret,
                            block_size=block,
                            bricks_held=bricks_in_trade,
                            fold_label=fold_label,
                        )
                    )
                position_open = False
                entry_brick = None
                bricks_in_trade = 0
        prev = b

    # If still open at end and entry was in fold window: mark-to-market.
    if position_open and entry_brick is not None:
        if fold_start <= entry_brick.time.date() < fold_end:
            last = bricks[-1]
            entry_price = float(entry_brick.close)
            exit_price = float(last.close)
            ret = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
            trades.append(
                Trade(
                    timeframe=timeframe_label,
                    underlying=str(meta["underlying"]),
                    expiry=str(meta["expiry"]),
                    strike=float(meta["strike"]),
                    option_type=str(meta["option_type"]),
                    entry_time=entry_brick.time.isoformat(),
                    entry_price=entry_price,
                    exit_time=last.time.isoformat(),
                    exit_price=exit_price,
                    exit_reason="end_of_data",
                    return_pct=ret,
                    block_size=block,
                    bricks_held=bricks_in_trade,
                    fold_label=fold_label,
                )
            )
    return trades


# ── Resample helper ─────────────────────────────────────────────────────────
def resample_ohlc(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    df = df.set_index("time").sort_index()
    out = pd.DataFrame(
        {
            "open": df["open"].resample(freq, label="left", closed="left").first(),
            "high": df["high"].resample(freq, label="left", closed="left").max(),
            "low": df["low"].resample(freq, label="left", closed="left").min(),
            "close": df["close"].resample(freq, label="left", closed="left").last(),
        }
    ).dropna()
    out = out.reset_index()
    return out


# ── DB loading ──────────────────────────────────────────────────────────────
async def load_contracts(session, interval_label: str) -> list[tuple]:
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT underlying, expiry, strike, option_type
                FROM option_premium_candles
                WHERE interval = :iv
                """
            ),
            {"iv": interval_label},
        )
    ).all()
    return [tuple(r) for r in rows]


async def load_bars(session, c, interval_label: str) -> pd.DataFrame:
    u, e, s, o = c
    rows = (
        await session.execute(
            text(
                """
                SELECT time, open, high, low, close
                FROM option_premium_candles
                WHERE underlying=:u AND expiry=:e AND strike=:s
                  AND option_type=:o AND interval=:iv
                ORDER BY time
                """
            ),
            {"u": u, "e": e, "s": s, "o": o, "iv": interval_label},
        )
    ).mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["time"])
    return df


# ── Walk-forward folds ──────────────────────────────────────────────────────
def weekly_folds(start: date, end: date) -> list[tuple[date, date]]:
    """Mondays partitioning [start, end). Each fold = [Mon, next-Mon)."""
    cur = start - timedelta(days=start.weekday())
    folds: list[tuple[date, date]] = []
    while cur < end:
        nxt = cur + timedelta(days=7)
        if cur >= start - timedelta(days=7):  # include even partial first week
            folds.append((cur, nxt))
        cur = nxt
    return folds


# ── Summary helpers ─────────────────────────────────────────────────────────
def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"n_trades": 0}
    rets = [t.return_pct for t in trades]
    wins = [r for r in rets if r > 0]
    holds = [t.bricks_held for t in trades]
    return {
        "n_trades": len(trades),
        "n_contracts": len({(t.underlying, t.expiry, t.strike, t.option_type) for t in trades}),
        "win_rate_pct": round(100.0 * len(wins) / len(trades), 2),
        "avg_return_pct": round(mean(rets), 2),
        "median_return_pct": round(median(rets), 2),
        "best_pct": round(max(rets), 2),
        "worst_pct": round(min(rets), 2),
        "median_bricks_held": int(median(holds)),
        "exit_breakdown": {
            "down_brick": sum(1 for t in trades if t.exit_reason == "down_brick"),
            "end_of_data": sum(1 for t in trades if t.exit_reason == "end_of_data"),
        },
    }


async def run_one_timeframe(session, db_interval: str, resample_freq: str | None, tf_label: str):
    contracts = await load_contracts(session, db_interval)
    print(f"  [{tf_label}] contracts: {len(contracts)}")
    if not contracts:
        return [], []

    # Find global date range to define folds.
    bounds_row = (
        await session.execute(
            text("SELECT MIN(time)::date AS s, MAX(time)::date AS e FROM option_premium_candles WHERE interval=:iv"),
            {"iv": db_interval},
        )
    ).mappings().first()
    folds = weekly_folds(bounds_row["s"], bounds_row["e"] + timedelta(days=1))
    print(f"  [{tf_label}] weekly folds: {len(folds)} (from {bounds_row['s']} to {bounds_row['e']})")

    all_trades: list[Trade] = []
    fold_rows: list[dict] = []

    for ci, c in enumerate(contracts, 1):
        df = await load_bars(session, c, db_interval)
        if df.empty:
            continue
        if resample_freq:
            df = resample_ohlc(df, resample_freq)
        if len(df) < ATR_PERIOD + MIN_BARS_AFTER_ATR:
            continue
        atr_series = atr(df)
        first_atr_idx = atr_series.first_valid_index()
        if first_atr_idx is None:
            continue
        block = float(atr_series.iloc[first_atr_idx])
        if block <= 0:
            continue
        df_trade = df.iloc[first_atr_idx:].reset_index(drop=True)
        bricks = build_renko(df_trade, block)
        if len(bricks) < 2:
            continue

        meta = {"underlying": c[0], "expiry": c[1], "strike": c[2], "option_type": c[3]}
        for f in folds:
            trades = simulate(bricks, f, tf_label, meta, block)
            all_trades.extend(trades)
        if ci % 200 == 0:
            print(f"  [{tf_label}] processed {ci}/{len(contracts)}; trades so far: {len(all_trades)}")

    # Per-fold summary
    for f in folds:
        flabel = f"{f[0].isoformat()}_to_{f[1].isoformat()}"
        ft = [t for t in all_trades if t.fold_label == flabel]
        s = summarize(ft)
        if s["n_trades"]:
            ce = summarize([t for t in ft if t.option_type == "CE"])
            pe = summarize([t for t in ft if t.option_type == "PE"])
            fold_rows.append(
                {
                    "fold": flabel,
                    "n_trades": s["n_trades"],
                    "win_rate_pct": s["win_rate_pct"],
                    "avg_return_pct": s["avg_return_pct"],
                    "median_return_pct": s["median_return_pct"],
                    "best_pct": s["best_pct"],
                    "worst_pct": s["worst_pct"],
                    "CE_n": ce.get("n_trades", 0),
                    "CE_avg": ce.get("avg_return_pct"),
                    "CE_wr": ce.get("win_rate_pct"),
                    "PE_n": pe.get("n_trades", 0),
                    "PE_avg": pe.get("avg_return_pct"),
                    "PE_wr": pe.get("win_rate_pct"),
                }
            )
    return all_trades, fold_rows


async def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "config": {
            "block_size": "1 × ATR(14) on the chosen timeframe, fixed per contract at first valid ATR",
            "rule_entry": "first UP brick after a DOWN brick → BUY at brick-printing bar's close",
            "rule_exit": "first DOWN brick after entry → SELL at that bar's close",
            "walk_forward": "weekly OOS folds; only entries inside the fold count toward fold stats",
            "long_only": True,
        }
    }
    async with AsyncSessionLocal() as session:
        for db_iv, resample, label in TIMEFRAMES:
            print(f"\n=== {label} renko ===")
            trades, folds = await run_one_timeframe(session, db_iv, resample, label)
            # Write CSVs.
            t_path = REPORT_DIR / f"trades_{label}.csv"
            with t_path.open("w", newline="") as f:
                if trades:
                    w = csv.DictWriter(f, fieldnames=list(asdict(trades[0]).keys()))
                    w.writeheader()
                    for t in trades:
                        w.writerow(asdict(t))
            f_path = REPORT_DIR / f"folds_{label}.csv"
            with f_path.open("w", newline="") as f:
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
    # Console summary
    for tf in ("15min", "30min"):
        if tf in out:
            print(f"\n=== {tf} OVERALL ===")
            print(json.dumps(out[tf]["overall"], indent=2))
            print(f"--- {tf} CE / PE ---")
            print("CE:", json.dumps(out[tf]["CE"], indent=2))
            print("PE:", json.dumps(out[tf]["PE"], indent=2))
            print(f"--- {tf} folds ---")
            for fr in out[tf]["folds"]:
                print(
                    f"  {fr['fold']}  n={fr['n_trades']:4}  wr={fr['win_rate_pct']:5}%  avg={fr['avg_return_pct']:6}%  med={fr['median_return_pct']:6}%  CE n/wr/avg={fr['CE_n']}/{fr['CE_wr']}/{fr['CE_avg']}  PE n/wr/avg={fr['PE_n']}/{fr['PE_wr']}/{fr['PE_avg']}"
                )
    print(f"\nwrote: {REPORT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
