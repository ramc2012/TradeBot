"""ATM CE/PE Renko sequential walk-forward over cached option data.

This is the multi-underlying companion to
research_nifty_atm_renko_15m_walkforward.py.

Use cases:
  - True 15-minute Renko where 1-minute or 5-minute option candles are cached:
      python -m scripts.research_atm_renko_cached_walkforward --source-interval 1minute

  - Jan-May broad cache test where only 30-minute option candles exist:
      python -m scripts.research_atm_renko_cached_walkforward --source-interval 30minute

The trading rule is unchanged:
  - When flat, observe only current ATM CE and current ATM PE for each symbol.
  - Enter long on a down-to-up Renko color change.
  - Hold until the same option prints an up-to-down color change.
  - After exit, recompute ATM and repeat.
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


REPORT_ROOT = Path(__file__).parent.parent / "reports"
ATR_PERIOD = 14
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


@dataclass(frozen=True)
class OptionKey:
    underlying: str
    strike: int
    option_type: str
    expiry: date


@dataclass
class Brick:
    time: pd.Timestamp
    close: float
    direction: str


@dataclass
class Trade:
    fold: str
    underlying: str
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


def atr(df: pd.DataFrame) -> pd.Series:
    return true_range(df).rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()


def resample_ohlc(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    src = df.set_index("time").sort_index()
    return pd.DataFrame(
        {
            "open": src["open"].resample(freq, label="left", closed="left").first(),
            "high": src["high"].resample(freq, label="left", closed="left").max(),
            "low": src["low"].resample(freq, label="left", closed="left").min(),
            "close": src["close"].resample(freq, label="left", closed="left").last(),
        }
    ).dropna().reset_index()


def build_renko(df: pd.DataFrame, block: float) -> list[Brick]:
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
    local = ts.tz_convert(IST).time()
    return MARKET_OPEN <= local <= MARKET_CLOSE


def weekly_folds(start: date, end: date) -> list[tuple[date, date]]:
    cur = start - timedelta(days=start.weekday())
    out: list[tuple[date, date]] = []
    while cur < end:
        nxt = cur + timedelta(days=7)
        out.append((cur, nxt))
        cur = nxt
    return out


def fold_label(day: date, folds: list[tuple[date, date]]) -> str:
    for start, end in folds:
        if start <= day < end:
            return f"{start.isoformat()}_to_{end.isoformat()}"
    return "unassigned"


def strike_step_for(underlying: str, strikes: list[int]) -> int:
    if underlying in {"BANKNIFTY", "SENSEX", "BANKEX"}:
        return 100
    if underlying in {"NIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}:
        return 50
    uniq = sorted(set(strikes))
    gaps = [b - a for a, b in zip(uniq, uniq[1:]) if b > a]
    return int(min(gaps)) if gaps else 1


def round_atm(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"n_trades": 0}
    pnls = [t.pnl_pct for t in trades]
    return {
        "n_trades": len(trades),
        "win_rate_pct": round(100 * sum(1 for p in pnls if p > 0) / len(pnls), 2),
        "sum_pnl_pct": round(sum(pnls), 2),
        "avg_pnl_pct": round(mean(pnls), 2),
        "median_pnl_pct": round(median(pnls), 2),
        "best_pct": round(max(pnls), 2),
        "worst_pct": round(min(pnls), 2),
        "median_hold_minutes": int(median([t.holding_minutes for t in trades])),
        "exit_breakdown": {
            reason: sum(1 for t in trades if t.exit_reason == reason)
            for reason in sorted({t.exit_reason for t in trades})
        },
    }


async def load_eligible_underlyings(
    session,
    source_interval: str,
    requested: list[str],
    from_date: date,
    to_date: date,
) -> list[str]:
    if isinstance(from_date, str):
        from_date = date.fromisoformat(from_date)
    if isinstance(to_date, str):
        to_date = date.fromisoformat(to_date)
    requested = [u.upper() for u in requested]
    if requested and requested != ["ALL"]:
        return requested
    rows = (
        await session.execute(
            text(
                """
                WITH option_underlyings AS (
                    SELECT DISTINCT underlying
                    FROM option_premium_candles
                    WHERE interval = :source_interval
                      AND time >= :from_date
                      AND time < (CAST(:to_date AS date) + INTERVAL '1 day')
                )
                SELECT o.underlying
                FROM option_underlyings o
                WHERE EXISTS (
                    SELECT 1
                    FROM underlying_spot_candles s
                    WHERE s.underlying = o.underlying
                      AND s.interval = '1minute'
                      AND s.close > 0
                      AND s.time >= :from_date
                      AND s.time < (CAST(:to_date AS date) + INTERVAL '1 day')
                )
                ORDER BY o.underlying
                """
            ),
            {"source_interval": source_interval, "from_date": from_date, "to_date": to_date},
        )
    ).scalars().all()
    return [str(r) for r in rows]


async def load_contracts(
    session,
    underlying: str,
    source_interval: str,
    from_date: date,
    to_date: date,
) -> list[OptionKey]:
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT strike, option_type, expiry
                FROM option_premium_candles
                WHERE underlying = :underlying
                  AND interval = :source_interval
                  AND time >= :from_date
                  AND time < (CAST(:to_date AS date) + INTERVAL '1 day')
                ORDER BY expiry, strike, option_type
                """
            ),
            {
                "underlying": underlying,
                "source_interval": source_interval,
                "from_date": from_date,
                "to_date": to_date,
            },
        )
    ).all()
    return [OptionKey(underlying, int(r[0]), str(r[1]), r[2]) for r in rows]


async def load_option_bars(
    session,
    key: OptionKey,
    source_interval: str,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
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
                  AND interval = :source_interval
                  AND time >= :from_date
                  AND time < (CAST(:to_date AS date) + INTERVAL '1 day')
                ORDER BY time
                """
            ),
            {
                "underlying": key.underlying,
                "expiry": key.expiry,
                "strike": key.strike,
                "option_type": key.option_type,
                "source_interval": source_interval,
                "from_date": from_date,
                "to_date": to_date,
            },
        )
    ).mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(row) for row in rows])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


async def load_spot(session, underlying: str, from_date: date, to_date: date) -> pd.DataFrame:
    rows = (
        await session.execute(
            text(
                """
                SELECT time, close
                FROM underlying_spot_candles
                WHERE underlying = :underlying
                  AND interval = '1minute'
                  AND close > 0
                  AND time >= :from_date
                  AND time < (CAST(:to_date AS date) + INTERVAL '1 day')
                ORDER BY time
                """
            ),
            {"underlying": underlying, "from_date": from_date, "to_date": to_date},
        )
    ).mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(row) for row in rows])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["close"] = df["close"].astype(float)
    return df.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)


def spot_at(spot: pd.DataFrame, when: pd.Timestamp) -> float | None:
    idx = spot["time"].searchsorted(when, side="right") - 1
    if idx < 0:
        return None
    return float(spot.iloc[idx]["close"])


def price_at(bars: pd.DataFrame, when: pd.Timestamp) -> float | None:
    idx = bars["time"].searchsorted(when, side="right") - 1
    if idx < 0:
        return None
    return float(bars.iloc[idx]["close"])


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        if not rows:
            return
        fieldnames = list(rows[0].keys())
        for row in rows[1:]:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


async def run_underlying(session, underlying: str, args: argparse.Namespace) -> tuple[list[Trade], dict]:
    contracts = await load_contracts(
        session,
        underlying,
        args.source_interval,
        date.fromisoformat(args.from_date),
        date.fromisoformat(args.to_date),
    )
    spot = await load_spot(
        session,
        underlying,
        date.fromisoformat(args.from_date),
        date.fromisoformat(args.to_date),
    )
    if not contracts or spot.empty:
        return [], {
            "underlying": underlying,
            "status": "skipped",
            "reason": "missing option contracts or spot candles",
            "contracts": len(contracts),
            "spot_rows": int(len(spot)),
        }

    step = strike_step_for(underlying, [c.strike for c in contracts])
    contract_map: dict[OptionKey, dict] = {}
    timeline: set[pd.Timestamp] = set()

    for key in contracts:
        raw = await load_option_bars(
            session,
            key,
            args.source_interval,
            date.fromisoformat(args.from_date),
            date.fromisoformat(args.to_date),
        )
        if raw.empty:
            continue
        bars = resample_ohlc(raw, "15min") if args.source_interval in {"1minute", "5minute"} else raw
        bars = bars[bars["time"].map(in_market_hours)].reset_index(drop=True)
        if len(bars) < ATR_PERIOD + args.min_bars_after_atr:
            continue
        atr_series = atr(bars)
        first_atr_idx = atr_series.first_valid_index()
        if first_atr_idx is None:
            continue
        block = float(atr_series.iloc[first_atr_idx])
        if block <= 0:
            continue
        trade_bars = bars.iloc[first_atr_idx:].reset_index(drop=True)
        bricks = build_renko(trade_bars, block)
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
        if entry_signals or exit_signals:
            timeline.update(entry_signals)
            timeline.update(exit_signals)
            contract_map[key] = {
                "block": block,
                "bars": trade_bars,
                "bricks": bricks,
                "entry_signals": entry_signals,
                "exit_signals": exit_signals,
            }

    if not contract_map or not timeline:
        return [], {
            "underlying": underlying,
            "status": "skipped",
            "reason": "no usable renko color-change signals",
            "contracts": len(contracts),
            "spot_rows": int(len(spot)),
            "usable_contracts": len(contract_map),
        }

    entry_index: dict[tuple[int, str, pd.Timestamp], list[OptionKey]] = {}
    for key, data in contract_map.items():
        for ts in data["entry_signals"]:
            entry_index.setdefault((key.strike, key.option_type, ts), []).append(key)
    for keys in entry_index.values():
        keys.sort(key=lambda k: k.expiry)

    folds = weekly_folds(date.fromisoformat(args.from_date), date.fromisoformat(args.to_date) + timedelta(days=1))
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
                pnl = ((exit_premium - entry_premium) / entry_premium * 100) if entry_premium > 0 else 0.0
                day = ist_date(entry_time)
                trades.append(
                    Trade(
                        fold=fold_label(day, folds),
                        underlying=underlying,
                        entry_day=day.isoformat(),
                        option_type=key.option_type,
                        strike=key.strike,
                        expiry=key.expiry.isoformat(),
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
                held_bricks = [b for b in contract_map[key]["bricks"] if b.time == ts]
                if held_bricks:
                    position["bricks_held"] += len(held_bricks)
            continue

        atm = round_atm(current_spot, step)
        day = ist_date(ts)
        for option_type in ("CE", "PE"):
            candidates = [
                key
                for key in entry_index.get((atm, option_type, ts), [])
                if key.expiry >= day and (key.expiry - day).days >= args.min_days_to_expiry
            ]
            if not candidates:
                continue
            key = candidates[0]
            brick = contract_map[key]["entry_signals"][ts]
            position = {
                "key": key,
                "entry_time": ts,
                "entry_premium": float(brick.close),
                "entry_spot": current_spot,
                "bricks_held": 1,
            }
            break

    if position is not None:
        key = position["key"]
        bars = contract_map[key]["bars"]
        last_ts = max(t for t in bars["time"] if t >= position["entry_time"])
        entry_premium = float(position["entry_premium"])
        exit_premium = price_at(bars, last_ts) or entry_premium
        exit_spot = spot_at(spot, last_ts) or 0.0
        pnl = ((exit_premium - entry_premium) / entry_premium * 100) if entry_premium > 0 else 0.0
        day = ist_date(position["entry_time"])
        trades.append(
            Trade(
                fold=fold_label(day, folds),
                underlying=underlying,
                entry_day=day.isoformat(),
                option_type=key.option_type,
                strike=key.strike,
                expiry=key.expiry.isoformat(),
                entry_time_ist=to_ist(position["entry_time"]),
                entry_premium=round(entry_premium, 2),
                entry_spot=round(float(position["entry_spot"]), 2),
                exit_time_ist=to_ist(last_ts),
                exit_premium=round(float(exit_premium), 2),
                exit_spot=round(float(exit_spot), 2),
                exit_reason="end_of_data",
                pnl_pct=round(pnl, 2),
                block_size=round(float(contract_map[key]["block"]), 2),
                bricks_held=int(position["bricks_held"]),
                holding_minutes=int((last_ts - position["entry_time"]).total_seconds() // 60),
            )
        )

    return trades, {
        "underlying": underlying,
        "status": "tested",
        "contracts": len(contracts),
        "spot_rows": int(len(spot)),
        "usable_contracts": len(contract_map),
        **summarize(trades),
    }


async def run(args: argparse.Namespace) -> dict:
    timeframe = "15min" if args.source_interval in {"1minute", "5minute"} else args.source_interval
    report_dir = REPORT_ROOT / f"research_atm_renko_cached_walkforward_{timeframe}_{args.from_date}_to_{args.to_date}"
    report_dir.mkdir(parents=True, exist_ok=True)

    async with AsyncSessionLocal() as session:
        underlyings = await load_eligible_underlyings(
            session,
            args.source_interval,
            args.underlying,
            args.from_date,
            args.to_date,
        )
        if args.max_underlyings:
            underlyings = underlyings[: args.max_underlyings]
        print(f"source={args.source_interval} timeframe={timeframe} underlyings={len(underlyings)}")

        all_trades: list[Trade] = []
        coverage: list[dict] = []
        for idx, underlying in enumerate(underlyings, 1):
            trades, row = await run_underlying(session, underlying, args)
            all_trades.extend(trades)
            coverage.append(row)
            print(
                f"[{idx}/{len(underlyings)}] {underlying}: {row['status']} "
                f"trades={row.get('n_trades', 0)} usable={row.get('usable_contracts', 0)}"
            )

    trade_rows = [asdict(t) for t in all_trades]
    write_csv(report_dir / "trades.csv", trade_rows)
    write_csv(report_dir / "coverage.csv", coverage)

    by_underlying = {
        row["underlying"]: row
        for row in coverage
        if row.get("status") == "tested"
    }
    by_month = {
        month: summarize([t for t in all_trades if t.entry_day.startswith(month)])
        for month in sorted({t.entry_day[:7] for t in all_trades})
    }
    by_fold = {
        fold: summarize([t for t in all_trades if t.fold == fold])
        for fold in sorted({t.fold for t in all_trades})
    }
    summary = {
        "config": {
            "source_interval": args.source_interval,
            "tested_timeframe": timeframe,
            "from_date": args.from_date,
            "to_date": args.to_date,
            "entry": "current ATM CE/PE only; long on down-to-up Renko color change",
            "exit": "same option exits on up-to-down Renko color change",
            "renko_block": "first valid ATR(14), fixed per option contract",
            "note": "30minute source is a Jan-May cache fallback, not a true 15-minute chart.",
        },
        "coverage": {
            "requested_underlyings": args.underlying,
            "tested_underlyings": sum(1 for row in coverage if row.get("status") == "tested"),
            "skipped_underlyings": sum(1 for row in coverage if row.get("status") != "tested"),
        },
        "overall": summarize(all_trades),
        "CE": summarize([t for t in all_trades if t.option_type == "CE"]),
        "PE": summarize([t for t in all_trades if t.option_type == "PE"]),
        "by_month": by_month,
        "by_fold": by_fold,
        "by_underlying": by_underlying,
    }
    (report_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print("\n=== OVERALL ===")
    print(json.dumps(summary["overall"], indent=2))
    print(f"\nwrote: {report_dir}")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-interval", choices=["1minute", "5minute", "30minute"], default="1minute")
    parser.add_argument("--from-date", default="2026-01-01")
    parser.add_argument("--to-date", default="2026-05-31")
    parser.add_argument("--underlying", nargs="+", default=["ALL"])
    parser.add_argument("--min-days-to-expiry", type=int, default=0)
    parser.add_argument("--min-bars-after-atr", type=int, default=5)
    parser.add_argument("--max-underlyings", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
