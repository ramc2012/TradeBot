"""Research: 2h MACD zero-cross entry, 30m MACD opposite-cross exit.

Premise:
  - Entry: BUY the option when its premium-MACD zero-line-crosses UP on a 2-hour bar
    (slow signal = stronger conviction, less noise).
  - Exit:  SELL when the SAME option's premium-MACD zero-line-crosses DOWN on a
    30-minute bar (fast signal = quick reaction when momentum turns).
  - Last-resort exit: end of candle data for that contract (assume exit at last bar).

Inputs: option_premium_candles, interval='30minute' (resampled to 2h by grouping
every 4 consecutive bars per contract — anchor on calendar bar order, not clock).

Output: aggregate stats + per-underlying breakdown + CSV of all trades.

Run:
    docker compose exec backend python -m scripts.research_2h_entry_30m_exit
"""
from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass, asdict
from pathlib import Path
from statistics import mean, median

import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_2h_30m"
MIN_30M_BARS_PER_CONTRACT = 60  # need ≥ 60 30m bars (≈ 15 2h bars) for MACD warmup
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9


@dataclass
class Trade:
    underlying: str
    expiry: str
    strike: float
    option_type: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    exit_reason: str  # "30m_cross_down" | "end_of_data"
    bars_held_30m: int
    return_pct: float


def compute_macd(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    ef = series.ewm(span=MACD_FAST, adjust=False).mean()
    es = series.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ef - es
    sig = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return macd, sig


def resample_to_2h(df_30m: pd.DataFrame) -> pd.DataFrame:
    """Bundle every 4 consecutive 30m bars into one 2h bar."""
    if len(df_30m) < 4:
        return pd.DataFrame()
    # Trim leading bars so length is a multiple of 4.
    cut = len(df_30m) - (len(df_30m) % 4)
    df = df_30m.iloc[:cut].copy().reset_index(drop=True)
    df["group"] = df.index // 4
    g = df.groupby("group", as_index=False).agg(
        time=("time", "first"),
        close=("close", "last"),
        end_time=("time", "last"),
    )
    return g


def find_zero_crosses(series: pd.Series, direction: str) -> list[int]:
    """Return integer indices where macd crosses zero in the given direction."""
    prev = series.shift(1)
    if direction == "up":
        return list(series.index[(prev < 0) & (series > 0)])
    return list(series.index[(prev > 0) & (series < 0)])


def simulate_contract(df_30m: pd.DataFrame) -> list[Trade]:
    """Run the 2h-entry / 30m-exit logic for one contract's 30m series."""
    df_30m = df_30m.sort_values("time").reset_index(drop=True)
    if len(df_30m) < MIN_30M_BARS_PER_CONTRACT:
        return []

    # 30-min MACD (used for exit timing).
    macd_30, _ = compute_macd(df_30m["close"])
    df_30m["macd_30"] = macd_30
    cross_down_30m_idx = find_zero_crosses(macd_30, "down")  # exit candidates

    # 2h resample + MACD (used for entry timing).
    df_2h = resample_to_2h(df_30m[["time", "close"]])
    if df_2h.empty or len(df_2h) < MACD_SLOW + MACD_SIGNAL:
        return []
    macd_2h, _ = compute_macd(df_2h["close"])
    cross_up_2h_idx = find_zero_crosses(macd_2h, "up")

    trades: list[Trade] = []
    last_exit_30m_idx = -1
    for ci in cross_up_2h_idx:
        # The 2h bar at index ci ENDS at end_time. We can only enter on the
        # NEXT 30m bar after that — translate ci back to the 30m index.
        end_time = df_2h.loc[ci, "end_time"]
        # Find first 30m bar with time > end_time.
        entry_candidates = df_30m.index[df_30m["time"] > end_time]
        if len(entry_candidates) == 0:
            continue
        entry_idx = int(entry_candidates[0])
        if entry_idx <= last_exit_30m_idx:
            # Avoid pyramiding — wait for previous trade to close.
            continue
        entry_row = df_30m.loc[entry_idx]

        # Walk forward for first 30m down-cross.
        next_exits = [i for i in cross_down_30m_idx if i > entry_idx]
        if next_exits:
            exit_idx = int(next_exits[0])
            exit_reason = "30m_cross_down"
        else:
            exit_idx = len(df_30m) - 1
            exit_reason = "end_of_data"
        exit_row = df_30m.loc[exit_idx]

        entry_price = float(entry_row["close"])
        exit_price = float(exit_row["close"])
        if entry_price <= 0:
            continue
        ret_pct = (exit_price - entry_price) / entry_price * 100.0

        trades.append(
            Trade(
                underlying=str(entry_row["underlying"]),
                expiry=str(entry_row["expiry"]),
                strike=float(entry_row["strike"]),
                option_type=str(entry_row["option_type"]),
                entry_time=entry_row["time"].isoformat(),
                entry_price=entry_price,
                exit_time=exit_row["time"].isoformat(),
                exit_price=exit_price,
                exit_reason=exit_reason,
                bars_held_30m=int(exit_idx - entry_idx),
                return_pct=ret_pct,
            )
        )
        last_exit_30m_idx = exit_idx
    return trades


async def load_contracts(session) -> list[tuple]:
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT underlying, expiry, strike, option_type
                FROM option_premium_candles
                WHERE interval = '30minute'
                """
            )
        )
    ).all()
    return [tuple(r) for r in rows]


async def load_contract_bars(session, c) -> pd.DataFrame:
    u, e, s, o = c
    rows = (
        await session.execute(
            text(
                """
                SELECT time, close, underlying, expiry, strike, option_type
                FROM option_premium_candles
                WHERE underlying = :u AND expiry = :e AND strike = :s
                  AND option_type = :o AND interval = '30minute'
                ORDER BY time
                """
            ),
            {"u": u, "e": e, "s": s, "o": o},
        )
    ).mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["close"] = df["close"].astype(float)
    return df


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t.return_pct for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    holds = [t.bars_held_30m for t in trades]
    return {
        "n_trades": len(trades),
        "n_contracts": len({(t.underlying, t.expiry, t.strike, t.option_type) for t in trades}),
        "win_rate_pct": round(100.0 * len(wins) / len(trades), 2),
        "avg_return_pct": round(mean(rets), 2),
        "median_return_pct": round(median(rets), 2),
        "best_pct": round(max(rets), 2),
        "worst_pct": round(min(rets), 2),
        "avg_winner_pct": round(mean(wins), 2) if wins else None,
        "avg_loser_pct": round(mean(losses), 2) if losses else None,
        "expectancy_pct": round(mean(rets), 2),
        "median_bars_held_30m": int(median(holds)),
        "exit_breakdown": {
            "30m_cross_down": sum(1 for t in trades if t.exit_reason == "30m_cross_down"),
            "end_of_data": sum(1 for t in trades if t.exit_reason == "end_of_data"),
        },
    }


async def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    all_trades: list[Trade] = []
    async with AsyncSessionLocal() as session:
        contracts = await load_contracts(session)
        print(f"contracts in universe: {len(contracts)}")
        for i, c in enumerate(contracts, 1):
            df = await load_contract_bars(session, c)
            if df.empty:
                continue
            all_trades.extend(simulate_contract(df))
            if i % 200 == 0:
                print(f"  progressed {i}/{len(contracts)}; trades so far: {len(all_trades)}")

    # Write CSV
    csv_path = REPORT_DIR / "trades.csv"
    with csv_path.open("w", newline="") as f:
        if all_trades:
            w = csv.DictWriter(f, fieldnames=list(asdict(all_trades[0]).keys()))
            w.writeheader()
            for t in all_trades:
                w.writerow(asdict(t))

    overall = summarize(all_trades)
    by_underlying = {}
    by_optype = {}
    by_reason_held = {}
    for u in sorted({t.underlying for t in all_trades}):
        by_underlying[u] = summarize([t for t in all_trades if t.underlying == u])
    for o in ("CE", "PE"):
        by_optype[o] = summarize([t for t in all_trades if t.option_type == o])
    # Held-by-30m exit (filter out end_of_data which inflate avgs).
    clean = [t for t in all_trades if t.exit_reason == "30m_cross_down"]
    by_reason_held["30m_cross_down_only"] = summarize(clean)

    summary = {
        "config": {
            "entry_signal": "2h MACD(12,26,9) zero-cross UP on option premium",
            "exit_signal": "30m MACD(12,26,9) zero-cross DOWN, or end-of-data",
            "universe": "all NSE 30m option premium candles",
            "data_window": "2026-01-28 → 2026-05-22",
        },
        "overall": overall,
        "by_option_type": by_optype,
        "clean_30m_exits_only": by_reason_held,
        "by_underlying_top10_by_trades": dict(
            sorted(by_underlying.items(), key=lambda kv: kv[1].get("n_trades", 0), reverse=True)[:10]
        ),
    }
    import json
    (REPORT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print()
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nwrote: {csv_path}")
    print(f"wrote: {REPORT_DIR / 'summary.json'}")


if __name__ == "__main__":
    asyncio.run(main())
