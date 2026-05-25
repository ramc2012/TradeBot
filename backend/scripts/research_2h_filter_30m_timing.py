"""Research v2: 2h MACD = regime filter, 30m MACD = entry/exit/re-entry timing.

Semantics (corrected per user clarification):
  - A contract is TRADABLE while its 2h MACD is above zero.
  - When 2h MACD crosses below zero → contract becomes untradable; force-close
    any open position at the next 30m bar.
  - Within a tradable window, the 30m MACD drives the trade:
        flat + 30m cross UP   → BUY at that 30m close
        long + 30m cross DOWN → SELL at that 30m close
        flat + next 30m cross UP → RE-ENTER (multiple round-trips allowed)
  - 2h MACD is sampled at each completed 2h bar; for any 30m bar the regime is
    whatever the most-recently-closed 2h bar dictated.

Run:
    docker compose exec backend python -m scripts.research_2h_filter_30m_timing
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


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_2h_filter_30m_timing"
MIN_30M_BARS_PER_CONTRACT = 60
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
    exit_reason: str  # "30m_cross_down" | "2h_regime_off" | "end_of_data"
    bars_held_30m: int
    return_pct: float
    cycle_in_window: int  # 1 = first entry in this 2h up-window, 2 = re-entry, etc.


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
        start_time=("time", "first"),
        end_time=("time", "last"),
        close=("close", "last"),
    )
    return g


def simulate_contract(df_30m: pd.DataFrame) -> list[Trade]:
    df_30m = df_30m.sort_values("time").reset_index(drop=True)
    if len(df_30m) < MIN_30M_BARS_PER_CONTRACT:
        return []

    # 30m MACD + cross flags.
    macd_30 = compute_macd(df_30m["close"])
    prev_30 = macd_30.shift(1)
    df_30m["cross_up_30"] = (prev_30 < 0) & (macd_30 > 0)
    df_30m["cross_down_30"] = (prev_30 > 0) & (macd_30 < 0)

    # 2h MACD on resampled bars.
    df_2h = resample_to_2h(df_30m[["time", "close"]])
    if df_2h.empty or len(df_2h) < MACD_SLOW + MACD_SIGNAL:
        return []
    macd_2h = compute_macd(df_2h["close"])
    df_2h["macd_2h"] = macd_2h
    df_2h["above_zero"] = macd_2h > 0
    # The 2h regime applies to all 30m bars whose time > this 2h end_time
    # and ≤ the next 2h end_time. Build a lookup: for each 30m bar, what
    # was the most recent completed 2h regime?
    df_2h_sorted = df_2h.sort_values("end_time").reset_index(drop=True)

    # Use merge_asof to attach the prevailing 2h regime to each 30m bar.
    left = df_30m[["time"]].copy().sort_values("time").reset_index(drop=True)
    right = df_2h_sorted[["end_time", "above_zero", "macd_2h"]].rename(columns={"end_time": "time"})
    merged = pd.merge_asof(
        left,
        right,
        on="time",
        direction="backward",
        allow_exact_matches=True,
    )
    df_30m["regime_tradable"] = merged["above_zero"].fillna(False).values

    # Walk the 30m series with a state machine.
    trades: list[Trade] = []
    position_open = False
    entry_idx: int | None = None
    cycle_in_window = 0
    last_regime = False  # to detect regime flip

    for i in range(len(df_30m)):
        row = df_30m.iloc[i]
        regime = bool(row["regime_tradable"])

        # Regime transition: if it just turned off, force-close any open trade.
        if last_regime and not regime and position_open:
            trades.append(_make_trade(df_30m, entry_idx, i, "2h_regime_off", cycle_in_window))
            position_open = False
            entry_idx = None
        # Reset cycle counter at the start of a fresh up-window.
        if regime and not last_regime:
            cycle_in_window = 0
        last_regime = regime

        if not regime:
            continue

        # Within a tradable window: trade off 30m crosses.
        if not position_open and bool(row["cross_up_30"]):
            entry_idx = i
            position_open = True
            cycle_in_window += 1
        elif position_open and bool(row["cross_down_30"]):
            trades.append(_make_trade(df_30m, entry_idx, i, "30m_cross_down", cycle_in_window))
            position_open = False
            entry_idx = None

    # If still open at end of data, mark-to-market exit.
    if position_open and entry_idx is not None:
        trades.append(_make_trade(df_30m, entry_idx, len(df_30m) - 1, "end_of_data", cycle_in_window))

    return trades


def _make_trade(df: pd.DataFrame, entry_idx: int, exit_idx: int, reason: str, cycle: int) -> Trade:
    er = df.iloc[entry_idx]
    xr = df.iloc[exit_idx]
    entry_price = float(er["close"])
    exit_price = float(xr["close"])
    ret = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
    return Trade(
        underlying=str(er["underlying"]),
        expiry=str(er["expiry"]),
        strike=float(er["strike"]),
        option_type=str(er["option_type"]),
        entry_time=er["time"].isoformat(),
        entry_price=entry_price,
        exit_time=xr["time"].isoformat(),
        exit_price=exit_price,
        exit_reason=reason,
        bars_held_30m=int(exit_idx - entry_idx),
        return_pct=ret,
        cycle_in_window=int(cycle),
    )


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
        return {"n_trades": 0}
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
            "2h_regime_off": sum(1 for t in trades if t.exit_reason == "2h_regime_off"),
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
            if i % 500 == 0:
                print(f"  {i}/{len(contracts)} contracts; trades so far: {len(all_trades)}")

    csv_path = REPORT_DIR / "trades.csv"
    with csv_path.open("w", newline="") as f:
        if all_trades:
            w = csv.DictWriter(f, fieldnames=list(asdict(all_trades[0]).keys()))
            w.writeheader()
            for t in all_trades:
                w.writerow(asdict(t))

    # By cycle: 1st entry vs re-entries
    by_cycle = {}
    for c in sorted({t.cycle_in_window for t in all_trades}):
        by_cycle[f"cycle_{c}"] = summarize([t for t in all_trades if t.cycle_in_window == c])

    summary = {
        "config": {
            "regime_filter": "contract tradable while 2h MACD(12,26,9) > 0",
            "entry": "30m MACD zero-cross UP within tradable regime (incl. re-entries)",
            "exit": "30m MACD zero-cross DOWN OR 2h regime flips off",
            "universe": "all NSE 30m option premium candles",
            "data_window": "2026-01-28 → 2026-05-22",
        },
        "overall": summarize(all_trades),
        "by_option_type": {o: summarize([t for t in all_trades if t.option_type == o]) for o in ("CE", "PE")},
        "by_cycle_in_window": by_cycle,
        "clean_30m_exits_only": summarize([t for t in all_trades if t.exit_reason == "30m_cross_down"]),
        "regime_off_exits_only": summarize([t for t in all_trades if t.exit_reason == "2h_regime_off"]),
        "by_underlying_top10": dict(
            sorted(
                {
                    u: summarize([t for t in all_trades if t.underlying == u])
                    for u in {t.underlying for t in all_trades}
                }.items(),
                key=lambda kv: kv[1].get("n_trades", 0),
                reverse=True,
            )[:10]
        ),
    }
    (REPORT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print()
    for k in ("overall", "by_option_type", "by_cycle_in_window", "clean_30m_exits_only", "regime_off_exits_only"):
        print(f"=== {k.upper()} ===")
        print(json.dumps(summary[k], indent=2))
        print()
    print(f"wrote: {csv_path}")
    print(f"wrote: {REPORT_DIR / 'summary.json'}")


if __name__ == "__main__":
    asyncio.run(main())
