"""NIFTY ATM CE/PE 15-minute Renko sequential walk-forward test.

Rules implemented from the user note:
  - When flat, observe only the current ATM CE and ATM PE.
  - Take one long option trade when either ATM option prints a Renko color
    change from down to up.
  - Hold that same option until its next Renko color change from up to down.
  - After exit, recompute the current ATM for that day/time and repeat.
  - One position at a time. No parallel CE/PE trades.

Walk-forward:
  - OOS folds are calendar weeks.
  - Trades are assigned to the fold containing their entry timestamp.
  - Renko block size is fixed from the first valid ATR(14) available in that
    contract's 15-minute history, avoiding full-contract future look-ahead.

Run:
    docker compose exec backend python -m scripts.research_nifty_atm_renko_15m_walkforward
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_nifty_atm_renko_15m_walkforward"
ATR_PERIOD = 14
STRIKE_STEP = 50
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


OptionKey = tuple[int, str, date]  # strike, option_type, expiry


@dataclass
class Brick:
    time: pd.Timestamp
    close: float
    direction: str  # "up" | "down"


@dataclass
class Trade:
    fold: str
    entry_day: str
    option_type: str
    strike: int
    expiry: str
    entry_time_ist: str
    entry_premium: float
    entry_spot: float
    exit_time_ist: str
    exit_premium: float
    exit_spot: float
    exit_reason: str
    pnl_pct: float
    block_size: float
    bricks_held: int
    holding_minutes: int


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    return true_range(df).rolling(period, min_periods=period).mean()


def resample_ohlc(df: pd.DataFrame, freq: str = "15min") -> pd.DataFrame:
    src = df.set_index("time").sort_index()
    out = pd.DataFrame(
        {
            "open": src["open"].resample(freq, label="left", closed="left").first(),
            "high": src["high"].resample(freq, label="left", closed="left").max(),
            "low": src["low"].resample(freq, label="left", closed="left").min(),
            "close": src["close"].resample(freq, label="left", closed="left").last(),
        }
    ).dropna()
    return out.reset_index()


def build_renko(df: pd.DataFrame, block: float) -> list[Brick]:
    """Close-driven directional Renko. Reversal requires a 2-block move."""
    if block <= 0 or df.empty:
        return []

    bricks: list[Brick] = []
    floor = float(df["close"].iloc[0])
    direction: str | None = None

    for _, row in df.iterrows():
        close = float(row["close"])
        if direction is None:
            if close >= floor + block:
                direction = "up"
                floor += block
                bricks.append(Brick(row["time"], close, "up"))
            elif close <= floor - block:
                direction = "down"
                floor -= block
                bricks.append(Brick(row["time"], close, "down"))
            continue

        if direction == "up":
            if close >= floor + block:
                n = int((close - floor) // block)
                floor += n * block
                bricks.append(Brick(row["time"], close, "up"))
            elif close <= floor - 2 * block:
                direction = "down"
                floor -= block
                bricks.append(Brick(row["time"], close, "down"))
        else:
            if close <= floor - block:
                n = int((floor - close) // block)
                floor -= n * block
                bricks.append(Brick(row["time"], close, "down"))
            elif close >= floor + 2 * block:
                direction = "up"
                floor += block
                bricks.append(Brick(row["time"], close, "up"))

    return bricks


def to_ist(ts: pd.Timestamp | datetime) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(IST).strftime("%Y-%m-%d %H:%M IST")


def ist_date(ts: pd.Timestamp | datetime) -> date:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(IST).date()


def in_market_hours(ts: pd.Timestamp) -> bool:
    local_time = ts.tz_convert(IST).time()
    return MARKET_OPEN <= local_time <= MARKET_CLOSE


def round_atm(spot: float, step: int = STRIKE_STEP) -> int:
    return int(round(spot / step) * step)


def weekly_folds(start: date, end: date) -> list[tuple[date, date]]:
    cur = start - timedelta(days=start.weekday())
    folds: list[tuple[date, date]] = []
    while cur < end:
        nxt = cur + timedelta(days=7)
        folds.append((cur, nxt))
        cur = nxt
    return folds


def fold_label_for(day: date, folds: list[tuple[date, date]]) -> str:
    for start, end in folds:
        if start <= day < end:
            return f"{start.isoformat()}_to_{end.isoformat()}"
    return "unassigned"


async def load_option_contracts(session, underlying: str) -> list[OptionKey]:
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT strike, option_type, expiry
                FROM option_premium_candles
                WHERE underlying = :underlying
                  AND interval = '5minute'
                ORDER BY expiry, strike, option_type
                """
            ),
            {"underlying": underlying},
        )
    ).all()
    return [(int(r[0]), str(r[1]), r[2]) for r in rows]


async def load_option_5m(session, underlying: str, key: OptionKey) -> pd.DataFrame:
    strike, option_type, expiry = key
    rows = (
        await session.execute(
            text(
                """
                SELECT time, open, high, low, close
                FROM option_premium_candles
                WHERE underlying = :underlying
                  AND expiry = :expiry
                  AND strike = :strike
                  AND option_type = :option_type
                  AND interval = '5minute'
                ORDER BY time
                """
            ),
            {
                "underlying": underlying,
                "expiry": expiry,
                "strike": strike,
                "option_type": option_type,
            },
        )
    ).mappings().all()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


async def load_spot(session, underlying: str) -> pd.DataFrame:
    """Use all cached spot intervals as an as-of source; denser rows win naturally."""
    rows = (
        await session.execute(
            text(
                """
                SELECT time, close
                FROM underlying_spot_candles
                WHERE underlying = :underlying
                  AND close > 10000
                ORDER BY time
                """
            ),
            {"underlying": underlying},
        )
    ).mappings().all()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["close"] = df["close"].astype(float)
    return df.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)


def spot_at(spot: pd.DataFrame, when: pd.Timestamp) -> float | None:
    if spot.empty:
        return None
    idx = spot["time"].searchsorted(when, side="right") - 1
    if idx < 0:
        return None
    return float(spot.iloc[idx]["close"])


def price_at(bar_df: pd.DataFrame, when: pd.Timestamp) -> float | None:
    idx = bar_df["time"].searchsorted(when, side="right") - 1
    if idx < 0:
        return None
    return float(bar_df.iloc[idx]["close"])


def summarize(trades: list[Trade], label: str | None = None) -> dict:
    if not trades:
        out = {"n_trades": 0}
        if label is not None:
            out["label"] = label
        return out

    pnls = [t.pnl_pct for t in trades]
    holds = [t.holding_minutes for t in trades]
    out = {
        "n_trades": len(trades),
        "win_rate_pct": round(100.0 * sum(1 for p in pnls if p > 0) / len(pnls), 2),
        "sum_pnl_pct": round(sum(pnls), 2),
        "avg_pnl_pct": round(mean(pnls), 2),
        "median_pnl_pct": round(median(pnls), 2),
        "best_pct": round(max(pnls), 2),
        "worst_pct": round(min(pnls), 2),
        "median_hold_minutes": int(median(holds)),
        "exit_breakdown": {
            reason: sum(1 for t in trades if t.exit_reason == reason)
            for reason in sorted({t.exit_reason for t in trades})
        },
    }
    if label is not None:
        out["label"] = label
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        if not rows:
            return
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


async def run(args: argparse.Namespace) -> dict:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    async with AsyncSessionLocal() as session:
        spot = await load_spot(session, args.underlying)
        if spot.empty:
            raise SystemExit(f"No spot candles found for {args.underlying}")

        contracts = await load_option_contracts(session, args.underlying)
        print(f"{args.underlying} 5-minute option contracts: {len(contracts)}")

        contract_map: dict[OptionKey, dict] = {}
        for idx, key in enumerate(contracts, 1):
            df5 = await load_option_5m(session, args.underlying, key)
            if df5.empty:
                continue

            df15 = resample_ohlc(df5, "15min")
            df15 = df15[df15["time"].map(in_market_hours)].reset_index(drop=True)
            if len(df15) < ATR_PERIOD + args.min_bars_after_atr:
                continue

            atr_series = atr(df15)
            first_atr_idx = atr_series.first_valid_index()
            if first_atr_idx is None:
                continue

            block = float(atr_series.iloc[first_atr_idx])
            if block <= 0:
                continue

            trade_df = df15.iloc[first_atr_idx:].reset_index(drop=True)
            bricks = build_renko(trade_df, block)
            if len(bricks) < 2:
                continue

            entry_signals: dict[pd.Timestamp, Brick] = {}
            exit_signals: dict[pd.Timestamp, Brick] = {}
            prev = bricks[0]
            for brick in bricks[1:]:
                if brick.direction != prev.direction:
                    if brick.direction == "up":
                        entry_signals[brick.time] = brick
                    else:
                        exit_signals[brick.time] = brick
                prev = brick

            contract_map[key] = {
                "block": block,
                "bars": trade_df,
                "bricks": bricks,
                "entry_signals": entry_signals,
                "exit_signals": exit_signals,
            }

            if idx % 100 == 0:
                print(f"processed {idx}/{len(contracts)} contracts; usable={len(contract_map)}")

        if not contract_map:
            raise SystemExit("No usable 15-minute Renko contracts found")

        entry_index: dict[tuple[int, str, pd.Timestamp], list[OptionKey]] = {}
        timeline: set[pd.Timestamp] = set()
        for key, data in contract_map.items():
            strike, option_type, _ = key
            for ts in data["entry_signals"]:
                entry_index.setdefault((strike, option_type, ts), []).append(key)
                timeline.add(ts)
            for ts in data["exit_signals"]:
                timeline.add(ts)

        for keys in entry_index.values():
            keys.sort(key=lambda k: k[2])

        start_day = min(ist_date(t) for t in timeline)
        end_day = max(ist_date(t) for t in timeline) + timedelta(days=1)
        folds = weekly_folds(start_day, end_day)
        print(f"usable Renko contracts: {len(contract_map)}")
        print(f"walk-forward folds: {len(folds)} ({start_day} to {end_day - timedelta(days=1)})")

        trades: list[Trade] = []
        position: dict | None = None

        for ts in sorted(timeline):
            if not in_market_hours(ts):
                continue

            current_spot = spot_at(spot, ts)
            if current_spot is None:
                continue

            if position is not None:
                key = position["key"]
                exit_brick = contract_map[key]["exit_signals"].get(ts)
                if exit_brick is not None:
                    entry_time = position["entry_time"]
                    entry_premium = float(position["entry_premium"])
                    exit_premium = float(exit_brick.close)
                    pnl = ((exit_premium - entry_premium) / entry_premium * 100.0) if entry_premium > 0 else 0.0
                    strike, option_type, expiry = key
                    entry_day = ist_date(entry_time)
                    trades.append(
                        Trade(
                            fold=fold_label_for(entry_day, folds),
                            entry_day=entry_day.isoformat(),
                            option_type=option_type,
                            strike=strike,
                            expiry=expiry.isoformat(),
                            entry_time_ist=to_ist(entry_time),
                            entry_premium=round(entry_premium, 2),
                            entry_spot=round(float(position["entry_spot"]), 2),
                            exit_time_ist=to_ist(ts),
                            exit_premium=round(exit_premium, 2),
                            exit_spot=round(current_spot, 2),
                            exit_reason="color_change_down",
                            pnl_pct=round(pnl, 2),
                            block_size=round(float(contract_map[key]["block"]), 2),
                            bricks_held=int(position["bricks_held"]) + 1,
                            holding_minutes=int((ts - entry_time).total_seconds() // 60),
                        )
                    )
                    position = None
                else:
                    # Count held-option bricks for diagnostics, including continuations.
                    held_bricks = [b for b in contract_map[key]["bricks"] if b.time == ts]
                    if held_bricks:
                        position["bricks_held"] += len(held_bricks)

                # Do not exit and re-enter on the same 15-minute bar.
                continue

            atm = round_atm(current_spot, args.strike_step)
            entry_day = ist_date(ts)
            entered = False
            for option_type in ("CE", "PE"):
                candidates = entry_index.get((atm, option_type, ts), [])
                eligible = [
                    key
                    for key in candidates
                    if key[2] >= entry_day
                    and (key[2] - entry_day).days >= args.min_days_to_expiry
                ]
                if not eligible:
                    continue

                key = eligible[0]
                brick = contract_map[key]["entry_signals"][ts]
                position = {
                    "key": key,
                    "entry_time": ts,
                    "entry_premium": float(brick.close),
                    "entry_spot": current_spot,
                    "bricks_held": 1,
                }
                entered = True
                break
            if entered:
                continue

        if position is not None:
            key = position["key"]
            last_ts = max(t for t in contract_map[key]["bars"]["time"] if t >= position["entry_time"])
            exit_spot = spot_at(spot, last_ts) or 0.0
            exit_premium = price_at(contract_map[key]["bars"], last_ts) or float(position["entry_premium"])
            entry_premium = float(position["entry_premium"])
            pnl = ((exit_premium - entry_premium) / entry_premium * 100.0) if entry_premium > 0 else 0.0
            strike, option_type, expiry = key
            entry_day = ist_date(position["entry_time"])
            trades.append(
                Trade(
                    fold=fold_label_for(entry_day, folds),
                    entry_day=entry_day.isoformat(),
                    option_type=option_type,
                    strike=strike,
                    expiry=expiry.isoformat(),
                    entry_time_ist=to_ist(position["entry_time"]),
                    entry_premium=round(entry_premium, 2),
                    entry_spot=round(float(position["entry_spot"]), 2),
                    exit_time_ist=to_ist(last_ts),
                    exit_premium=round(exit_premium, 2),
                    exit_spot=round(exit_spot, 2),
                    exit_reason="end_of_data",
                    pnl_pct=round(pnl, 2),
                    block_size=round(float(contract_map[key]["block"]), 2),
                    bricks_held=int(position["bricks_held"]),
                    holding_minutes=int((last_ts - position["entry_time"]).total_seconds() // 60),
                )
            )

        trade_rows = [asdict(t) for t in trades]

        fold_rows: list[dict] = []
        for start, end in folds:
            label = f"{start.isoformat()}_to_{end.isoformat()}"
            fold_trades = [t for t in trades if t.fold == label]
            if not fold_trades:
                continue
            row = {"fold": label, **summarize(fold_trades)}
            for option_type in ("CE", "PE"):
                side_summary = summarize([t for t in fold_trades if t.option_type == option_type])
                row[f"{option_type}_n"] = side_summary["n_trades"]
                row[f"{option_type}_avg"] = side_summary.get("avg_pnl_pct")
                row[f"{option_type}_wr"] = side_summary.get("win_rate_pct")
            fold_rows.append(row)

        day_rows: list[dict] = []
        for day in sorted({t.entry_day for t in trades}):
            day_trades = [t for t in trades if t.entry_day == day]
            day_rows.append({"day": day, **summarize(day_trades)})

        month_summary = {}
        for month in sorted({t.entry_day[:7] for t in trades}):
            month_summary[month] = summarize([t for t in trades if t.entry_day.startswith(month)])

        summary = {
            "config": {
                "underlying": args.underlying,
                "source_option_interval": "5minute",
                "renko_timeframe": "15minute",
                "renko_block": "first valid ATR(14) on 15-minute option OHLC, fixed per contract",
                "entry": "when flat, current ATM CE/PE only; buy first side that prints down-to-up color change",
                "exit": "hold same option until up-to-down color change; end_of_data only if no later color change exists",
                "strike_step": args.strike_step,
                "min_days_to_expiry": args.min_days_to_expiry,
                "walk_forward": "weekly OOS folds by entry date; no optimized parameter is fit on future data",
                "tie_break": "CE before PE when both current-ATM sides signal on the same bar",
            },
            "data": {
                "spot_rows": int(len(spot)),
                "raw_option_contracts": int(len(contracts)),
                "usable_renko_contracts": int(len(contract_map)),
                "first_signal_day": start_day.isoformat(),
                "last_signal_day": (end_day - timedelta(days=1)).isoformat(),
            },
            "overall": summarize(trades),
            "CE": summarize([t for t in trades if t.option_type == "CE"]),
            "PE": summarize([t for t in trades if t.option_type == "PE"]),
            "by_month": month_summary,
            "folds": fold_rows,
            "days": day_rows,
        }

        write_csv(REPORT_DIR / "trades.csv", trade_rows)
        write_csv(REPORT_DIR / "folds.csv", fold_rows)
        write_csv(REPORT_DIR / "days.csv", day_rows)
        (REPORT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

        print("\n=== OVERALL ===")
        print(json.dumps(summary["overall"], indent=2))
        print("\n=== CE ===")
        print(json.dumps(summary["CE"], indent=2))
        print("\n=== PE ===")
        print(json.dumps(summary["PE"], indent=2))
        print("\n=== FOLDS ===")
        for row in fold_rows:
            print(
                f"  {row['fold']} n={row['n_trades']:3} "
                f"wr={row['win_rate_pct']:5}% avg={row['avg_pnl_pct']:7}% "
                f"med={row['median_pnl_pct']:7}% CE={row['CE_n']}/{row['CE_wr']}/{row['CE_avg']} "
                f"PE={row['PE_n']}/{row['PE_wr']}/{row['PE_avg']}"
            )
        print(f"\nwrote: {REPORT_DIR}")

        return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--strike-step", type=int, default=STRIKE_STEP)
    parser.add_argument("--min-days-to-expiry", type=int, default=0)
    parser.add_argument("--min-bars-after-atr", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
