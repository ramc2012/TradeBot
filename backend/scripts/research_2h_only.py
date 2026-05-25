"""Research v3: pure 2h MACD cycle.

Entry: 2h MACD(12,26,9) zero-cross UP on option premium → BUY at that 2h close.
Exit:  next 2h MACD zero-cross DOWN → SELL at that 2h close.
End-of-data: mark-to-market exit at last 2h bar.

Also runs three sensible "reasonable exit" overlays:
  • baseline  : pure 2h up → 2h down (no other exit)
  • stop_25   : pure 2h cycle OR −25% stop on entry premium (whichever first)
  • take_50   : pure 2h cycle OR +50% profit-take (whichever first)
  • trail_50  : pure 2h cycle OR 50% giveback of peak unrealized gain

Run:
    docker compose exec backend python -m scripts.research_2h_only
"""
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from statistics import mean, median

import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_2h_only"
MIN_30M_BARS = 60
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
    exit_reason: str
    bars_held_2h: int
    return_pct: float
    peak_unrealized_pct: float


def compute_macd(series: pd.Series) -> pd.Series:
    ef = series.ewm(span=MACD_FAST, adjust=False).mean()
    es = series.ewm(span=MACD_SLOW, adjust=False).mean()
    return ef - es


def resample_to_2h(df_30m: pd.DataFrame) -> pd.DataFrame:
    if len(df_30m) < 4:
        return pd.DataFrame()
    cut = len(df_30m) - (len(df_30m) % 4)
    df = df_30m.iloc[:cut].copy().reset_index(drop=True)
    df["group"] = df.index // 4
    g = df.groupby("group", as_index=False).agg(
        time=("time", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    )
    return g


def simulate_contract(df_30m_in: pd.DataFrame, exit_mode: str, meta: dict) -> list[Trade]:
    """exit_mode ∈ {baseline, stop_25, take_50, trail_50}. `meta` carries the
    contract identity (underlying, expiry, strike, option_type) since resample
    drops those columns."""
    if len(df_30m_in) < MIN_30M_BARS:
        return []
    df_2h = resample_to_2h(df_30m_in)
    if df_2h.empty or len(df_2h) < MACD_SLOW + MACD_SIGNAL:
        return []
    macd = compute_macd(df_2h["close"])
    prev = macd.shift(1)
    up_cross = (prev < 0) & (macd > 0)
    down_cross = (prev > 0) & (macd < 0)

    trades: list[Trade] = []
    n = len(df_2h)
    i = 0
    while i < n:
        if not up_cross.iloc[i]:
            i += 1
            continue
        entry_idx = i
        entry_row = df_2h.iloc[entry_idx]
        entry_price = float(entry_row["close"])
        if entry_price <= 0:
            i += 1
            continue

        # Walk forward to find exit.
        exit_idx = None
        exit_reason = ""
        peak = entry_price
        for j in range(entry_idx + 1, n):
            row = df_2h.iloc[j]
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            peak = max(peak, high)
            cur_pct = (close - entry_price) / entry_price * 100.0

            # Custom exits (intra-bar approximations using high/low).
            if exit_mode == "stop_25" and low <= entry_price * 0.75:
                exit_idx = j
                exit_reason = "stop_25"
                # Approximate fill at the stop level, not the close.
                trades.append(Trade(
                    underlying=str(meta["underlying"]),
                    expiry=str(meta["expiry"]),
                    strike=float(meta["strike"]),
                    option_type=str(meta["option_type"]),
                    entry_time=entry_row["time"].isoformat(),
                    entry_price=entry_price,
                    exit_time=row["time"].isoformat(),
                    exit_price=entry_price * 0.75,
                    exit_reason="stop_25",
                    bars_held_2h=int(j - entry_idx),
                    return_pct=-25.0,
                    peak_unrealized_pct=(peak - entry_price) / entry_price * 100.0,
                ))
                break
            if exit_mode == "take_50" and high >= entry_price * 1.50:
                trades.append(Trade(
                    underlying=str(meta["underlying"]),
                    expiry=str(meta["expiry"]),
                    strike=float(meta["strike"]),
                    option_type=str(meta["option_type"]),
                    entry_time=entry_row["time"].isoformat(),
                    entry_price=entry_price,
                    exit_time=row["time"].isoformat(),
                    exit_price=entry_price * 1.50,
                    exit_reason="take_50",
                    bars_held_2h=int(j - entry_idx),
                    return_pct=50.0,
                    peak_unrealized_pct=(peak - entry_price) / entry_price * 100.0,
                ))
                exit_idx = j
                break
            if exit_mode == "trail_50":
                peak_gain_pct = (peak - entry_price) / entry_price * 100.0
                # Activate trail only after +20% has been touched.
                if peak_gain_pct >= 20.0:
                    giveback_trigger = entry_price + (peak - entry_price) * 0.5
                    if low <= giveback_trigger:
                        trades.append(Trade(
                            underlying=str(meta["underlying"]),
                            expiry=str(meta["expiry"]),
                            strike=float(meta["strike"]),
                            option_type=str(meta["option_type"]),
                            entry_time=entry_row["time"].isoformat(),
                            entry_price=entry_price,
                            exit_time=row["time"].isoformat(),
                            exit_price=giveback_trigger,
                            exit_reason="trail_50",
                            bars_held_2h=int(j - entry_idx),
                            return_pct=(giveback_trigger - entry_price) / entry_price * 100.0,
                            peak_unrealized_pct=peak_gain_pct,
                        ))
                        exit_idx = j
                        break

            # Baseline exit: 2h cross down.
            if down_cross.iloc[j]:
                trades.append(Trade(
                    underlying=str(meta["underlying"]),
                    expiry=str(meta["expiry"]),
                    strike=float(meta["strike"]),
                    option_type=str(meta["option_type"]),
                    entry_time=entry_row["time"].isoformat(),
                    entry_price=entry_price,
                    exit_time=row["time"].isoformat(),
                    exit_price=close,
                    exit_reason="2h_cross_down",
                    bars_held_2h=int(j - entry_idx),
                    return_pct=(close - entry_price) / entry_price * 100.0,
                    peak_unrealized_pct=(peak - entry_price) / entry_price * 100.0,
                ))
                exit_idx = j
                break

        # End-of-data fallback.
        if exit_idx is None:
            last = df_2h.iloc[-1]
            close = float(last["close"])
            trades.append(Trade(
                underlying=str(meta["underlying"]),
                expiry=str(meta["expiry"]),
                strike=float(meta["strike"]),
                option_type=str(meta["option_type"]),
                entry_time=entry_row["time"].isoformat(),
                entry_price=entry_price,
                exit_time=last["time"].isoformat(),
                exit_price=close,
                exit_reason="end_of_data",
                bars_held_2h=int(n - 1 - entry_idx),
                return_pct=(close - entry_price) / entry_price * 100.0,
                peak_unrealized_pct=(peak - entry_price) / entry_price * 100.0,
            ))
            exit_idx = n - 1

        # Don't pyramid: next entry must be after the exit.
        i = exit_idx + 1
    return trades


async def load_contracts(session):
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


async def load_bars(session, c) -> pd.DataFrame:
    u, e, s, o = c
    rows = (
        await session.execute(
            text(
                """
                SELECT time, high, low, close, underlying, expiry, strike, option_type
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
    for col in ("high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"n_trades": 0}
    rets = [t.return_pct for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    holds = [t.bars_held_2h for t in trades]
    peaks = [t.peak_unrealized_pct for t in trades]
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
        "median_bars_held_2h": int(median(holds)),
        "median_peak_unrealized_pct": round(median(peaks), 2),
        "exit_breakdown": {
            k: sum(1 for t in trades if t.exit_reason == k)
            for k in {"2h_cross_down", "stop_25", "take_50", "trail_50", "end_of_data"}
        },
    }


async def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    modes = ["baseline", "stop_25", "take_50", "trail_50"]
    results: dict[str, list[Trade]] = {m: [] for m in modes}

    async with AsyncSessionLocal() as session:
        contracts = await load_contracts(session)
        print(f"contracts in universe: {len(contracts)}")
        for i, c in enumerate(contracts, 1):
            df = await load_bars(session, c)
            if df.empty:
                continue
            meta = {"underlying": c[0], "expiry": c[1], "strike": c[2], "option_type": c[3]}
            for m in modes:
                results[m].extend(simulate_contract(df, exit_mode=m, meta=meta))
            if i % 500 == 0:
                print(f"  {i}/{len(contracts)} contracts")

    # Persist per-mode csv + combined summary.
    summary = {
        "config": {
            "entry": "2h MACD(12,26,9) zero-cross UP on option premium",
            "data_window": "2026-01-28 → 2026-05-22",
            "universe": "all NSE 30m option premium candles, resampled to 2h",
            "exit_modes": {
                "baseline": "2h MACD zero-cross DOWN (no other exit)",
                "stop_25": "baseline OR −25% stop on entry premium",
                "take_50": "baseline OR +50% profit-take",
                "trail_50": "baseline OR after peak >= +20%, exit if price gives back 50% of peak gain",
            },
        },
    }
    for m, trades in results.items():
        (REPORT_DIR / f"trades_{m}.csv").open("w", newline="").write("")
        with (REPORT_DIR / f"trades_{m}.csv").open("w", newline="") as f:
            if trades:
                w = csv.DictWriter(f, fieldnames=list(asdict(trades[0]).keys()))
                w.writeheader()
                for t in trades:
                    w.writerow(asdict(t))
        summary[m] = {
            "overall": summarize(trades),
            "CE": summarize([t for t in trades if t.option_type == "CE"]),
            "PE": summarize([t for t in trades if t.option_type == "PE"]),
        }

    (REPORT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print()
    for m in modes:
        print(f"=== MODE: {m} ===")
        print(json.dumps(summary[m]["overall"], indent=2))
        print()
    print(f"wrote: {REPORT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
