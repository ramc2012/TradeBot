"""NIFTY Mar-May 2026 — sequential ATM Renko, both A (full size) and B (scaled).

Strategy:
  - Each bar tick: compute current ATM from NIFTY 30-min spot.
  - Pick the (strike, expiry) contract that's ATM and has data.
    Expiry rotation: use nearest expiry that's >= 5 days out.
  - Renko per contract, brick = median rolling ATR(14) on 30-min OHLC.
  - Sequential, one position at a time.

Version A — Standard size (or 50% if --halfsize flag used):
  - Enter full position on first reversal brick (UP after DOWN).
  - Exit full position on first opposite-direction brick.

Version B — Scaling:
  - First UP brick after DOWN  → enter 50% position.
  - Second consecutive UP brick → add 50% (now 100%).
  - First DOWN brick after UPs → exit 50% (partial scale-out).
  - Second DOWN brick           → exit remaining 50% (full flat).

Note: Jan/Feb 2026 NIFTY traded at 25-26k and we have no strike data above
24,050. Those months are not tradable from current DB.

Run:
    docker compose exec backend python -m scripts.research_nifty_atm_seq_mar_to_may
"""
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal


REPORT_DIR = Path(__file__).parent.parent / "reports" / "research_nifty_atm_seq_mar_to_may"
ATR_PERIOD = 14
STRIKE_STEP = 50
IST = ZoneInfo("Asia/Kolkata")
MIN_DAYS_TO_EXPIRY = 5  # don't trade expiries closer than this


@dataclass
class TradeA:
    """Version A: single full-size entry & exit."""
    day: str
    side: str
    strike: int
    expiry: str
    entry_time_ist: str
    entry_premium: float
    entry_spot: float
    exit_time_ist: str
    exit_premium: float
    exit_spot: float
    pnl_pct: float
    block_size: float
    bricks_held: int
    holding_minutes: int


@dataclass
class TradeB:
    """Version B: scaling — 2 entry legs, 2 exit legs, blended fills."""
    day: str
    side: str
    strike: int
    expiry: str
    entry1_time_ist: str
    entry1_premium: float
    entry2_time_ist: str  # may be same as exit1 if no second add
    entry2_premium: float
    avg_entry_premium: float
    exit1_time_ist: str
    exit1_premium: float
    exit2_time_ist: str
    exit2_premium: float
    avg_exit_premium: float
    pnl_pct: float
    bricks_held: int
    holding_minutes: int


def true_range(df):
    pc = df["close"].shift(1)
    return pd.concat([df["high"]-df["low"], (df["high"]-pc).abs(), (df["low"]-pc).abs()], axis=1).max(axis=1)


def atr(df, period=ATR_PERIOD):
    return true_range(df).rolling(period, min_periods=period).mean()


@dataclass
class Brick:
    time: pd.Timestamp
    close: float
    direction: str


def build_renko(df, block):
    if block <= 0 or len(df) == 0:
        return []
    bricks = []
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


def to_ist(t):
    if hasattr(t, "tz_convert"):
        return t.tz_convert(IST).strftime("%Y-%m-%d %H:%M IST")
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo("UTC"))
    return t.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


async def load_contracts(session) -> list[dict]:
    rows = (await session.execute(text("""
        SELECT DISTINCT strike, option_type, expiry
        FROM option_premium_candles
        WHERE underlying='NIFTY' AND interval='30minute'
          AND expiry <= '2026-05-31' AND expiry >= '2026-03-01'
        ORDER BY expiry, strike, option_type
    """))).all()
    return [{"strike": int(r[0]), "opt": r[1], "expiry": r[2]} for r in rows]


async def load_option_30m(session, strike, opt, expiry):
    rows = (await session.execute(text("""
        SELECT time, open, high, low, close
        FROM option_premium_candles
        WHERE underlying='NIFTY' AND expiry=:e AND strike=:s
          AND option_type=:o AND interval='30minute'
        ORDER BY time
    """), {"e": expiry, "s": strike, "o": opt})).mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["time"])
    return df


async def load_spot_30m(session):
    rows = (await session.execute(text("""
        SELECT time, close FROM underlying_spot_candles
        WHERE underlying='NIFTY' AND interval='30minute'
          AND instrument_key='NSE_INDEX|Nifty 50' AND close>10000
        ORDER BY time
    """))).mappings().all()
    df = pd.DataFrame([dict(r) for r in rows])
    df["close"] = df["close"].astype(float)
    df["time"] = pd.to_datetime(df["time"])
    return df


def find_eligible_contract(atm: int, t: pd.Timestamp, contract_map: dict, opt: str) -> tuple | None:
    """Among contracts where (strike=atm, opt) has a brick AT time t, pick the
    one with the nearest expiry that is still >= MIN_DAYS_TO_EXPIRY ahead."""
    candidates = []
    for key, data in contract_map.items():
        s, o, e = key
        if s != atm or o != opt:
            continue
        days_to_exp = (e - t.date()).days
        if days_to_exp < MIN_DAYS_TO_EXPIRY:
            continue
        if t in data["brick_index"]:
            candidates.append((days_to_exp, key))
    if not candidates:
        return None
    candidates.sort()  # nearest expiry first (smallest days_to_exp)
    return candidates[0][1]


def prev_brick_direction(bricks: list[Brick], t: pd.Timestamp) -> str | None:
    """Direction of the most recent brick strictly before t."""
    for i in range(len(bricks) - 1, -1, -1):
        if bricks[i].time < t:
            return bricks[i].direction
    return None


async def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    async with AsyncSessionLocal() as session:
        spot = await load_spot_30m(session)
        if spot.empty:
            print("no spot")
            return
        print(f"spot bars: {len(spot)}  {to_ist(spot['time'].min())} → {to_ist(spot['time'].max())}")

        all_contracts = await load_contracts(session)
        print(f"contract triples (strike, opt, expiry): {len(all_contracts)}")

        # Pre-build Renko for each unique (strike, opt, expiry).
        contract_map: dict[tuple[int, str, date], dict] = {}
        for c in all_contracts:
            key = (c["strike"], c["opt"], c["expiry"])
            df = await load_option_30m(session, c["strike"], c["opt"], c["expiry"])
            if len(df) < ATR_PERIOD + 5:
                continue
            a = atr(df)
            valid = a.dropna()
            if valid.empty:
                continue
            block = float(valid.median())
            if block <= 0:
                continue
            first_idx = a.first_valid_index()
            trade_df = df.iloc[first_idx:].reset_index(drop=True)
            bricks = build_renko(trade_df, block)
            if len(bricks) < 2:
                continue
            contract_map[key] = {
                "block": block,
                "bricks": bricks,
                "brick_index": {b.time: b for b in bricks},
            }
        print(f"contracts with usable Renko: {len(contract_map)}")

        # ── A: full-size sequential ─────────────────────────────────────────
        trades_a: list[TradeA] = []
        # ── B: scaling — 50% on first reversal, 50% on confirmation; scale-out
        # mirror — exit 50% on first opposite, 50% on confirmation.
        trades_b: list[TradeB] = []

        # State machines (separate for A and B so they run independently)
        state_a = None  # dict or None
        state_b = None

        # Walk spot timeline (each 30-min bar) and let either strategy enter/exit
        for _, srow in spot.iterrows():
            t = srow["time"]
            s = float(srow["close"])
            atm = int(round(s / STRIKE_STEP) * STRIKE_STEP)

            # ── A logic ──
            if state_a is None:
                # Look for entry on ATM CE OR ATM PE: first UP brick after DOWN brick.
                for opt in ("CE", "PE"):
                    key = find_eligible_contract(atm, t, contract_map, opt)
                    if key is None:
                        continue
                    bricks = contract_map[key]["bricks"]
                    b = contract_map[key]["brick_index"].get(t)
                    if b is None or b.direction != "up":
                        continue
                    prev = prev_brick_direction(bricks, t)
                    if prev != "down":
                        continue
                    state_a = {
                        "key": key, "entry_time": t, "entry_premium": float(b.close),
                        "entry_spot": s, "bricks_held": 1,
                    }
                    break
            else:
                key = state_a["key"]
                b = contract_map[key]["brick_index"].get(t)
                if b is not None:
                    state_a["bricks_held"] += 1
                    if b.direction == "down":
                        # Exit
                        entry_t = state_a["entry_time"]
                        entry_p = state_a["entry_premium"]
                        exit_t = t
                        exit_p = float(b.close)
                        pnl = ((exit_p - entry_p) / entry_p * 100.0) if entry_p > 0 else 0.0
                        strike, opt, expiry = key
                        trades_a.append(TradeA(
                            day=to_ist(entry_t).split()[0],
                            side=opt, strike=strike, expiry=expiry.isoformat(),
                            entry_time_ist=to_ist(entry_t), entry_premium=entry_p,
                            entry_spot=round(state_a["entry_spot"], 2),
                            exit_time_ist=to_ist(exit_t), exit_premium=exit_p,
                            exit_spot=round(s, 2),
                            pnl_pct=round(pnl, 2),
                            block_size=round(contract_map[key]["block"], 2),
                            bricks_held=state_a["bricks_held"],
                            holding_minutes=int((exit_t - entry_t).total_seconds() // 60),
                        ))
                        state_a = None

            # ── B logic ──
            if state_b is None:
                # Look for ENTRY1 (first UP after DOWN) on ATM CE or PE.
                for opt in ("CE", "PE"):
                    key = find_eligible_contract(atm, t, contract_map, opt)
                    if key is None:
                        continue
                    bricks = contract_map[key]["bricks"]
                    b = contract_map[key]["brick_index"].get(t)
                    if b is None or b.direction != "up":
                        continue
                    prev = prev_brick_direction(bricks, t)
                    if prev != "down":
                        continue
                    state_b = {
                        "phase": "leg1", "key": key,
                        "e1_time": t, "e1_premium": float(b.close), "e1_spot": s,
                        "e2_time": None, "e2_premium": None,
                        "x1_time": None, "x1_premium": None,
                        "bricks_held": 1,
                    }
                    break
            else:
                key = state_b["key"]
                b = contract_map[key]["brick_index"].get(t)
                if b is None:
                    continue
                state_b["bricks_held"] += 1
                ph = state_b["phase"]
                if ph == "leg1":
                    if b.direction == "up":
                        # 2nd consecutive up → scale in
                        state_b["e2_time"] = t
                        state_b["e2_premium"] = float(b.close)
                        state_b["phase"] = "full"
                    elif b.direction == "down":
                        # Exited before confirmation — only 50% was on; full exit at this brick.
                        e1_p = state_b["e1_premium"]
                        x_p = float(b.close)
                        avg_entry = e1_p
                        avg_exit = x_p
                        # Half position size: pnl on 50% notional
                        pnl_half = ((x_p - e1_p) / e1_p * 100.0) * 0.5 if e1_p > 0 else 0.0
                        strike, opt, expiry = key
                        trades_b.append(TradeB(
                            day=to_ist(state_b["e1_time"]).split()[0],
                            side=opt, strike=strike, expiry=expiry.isoformat(),
                            entry1_time_ist=to_ist(state_b["e1_time"]),
                            entry1_premium=e1_p,
                            entry2_time_ist="(no add — exited before confirmation)",
                            entry2_premium=0.0,
                            avg_entry_premium=avg_entry,
                            exit1_time_ist=to_ist(t),
                            exit1_premium=x_p,
                            exit2_time_ist="(50% only — no second leg active)",
                            exit2_premium=0.0,
                            avg_exit_premium=avg_exit,
                            pnl_pct=round(pnl_half, 2),
                            bricks_held=state_b["bricks_held"],
                            holding_minutes=int((t - state_b["e1_time"]).total_seconds() // 60),
                        ))
                        state_b = None
                elif ph == "full":
                    if b.direction == "down":
                        # First down → scale out 50%
                        state_b["x1_time"] = t
                        state_b["x1_premium"] = float(b.close)
                        state_b["phase"] = "half_exited"
                elif ph == "half_exited":
                    if b.direction == "down":
                        # Second down → exit remaining 50%
                        e1_p = state_b["e1_premium"]
                        e2_p = state_b["e2_premium"]
                        x1_p = state_b["x1_premium"]
                        x2_p = float(b.close)
                        avg_entry = (e1_p + e2_p) / 2.0
                        avg_exit = (x1_p + x2_p) / 2.0
                        pnl = ((avg_exit - avg_entry) / avg_entry * 100.0) if avg_entry > 0 else 0.0
                        strike, opt, expiry = key
                        trades_b.append(TradeB(
                            day=to_ist(state_b["e1_time"]).split()[0],
                            side=opt, strike=strike, expiry=expiry.isoformat(),
                            entry1_time_ist=to_ist(state_b["e1_time"]),
                            entry1_premium=e1_p,
                            entry2_time_ist=to_ist(state_b["e2_time"]),
                            entry2_premium=e2_p,
                            avg_entry_premium=round(avg_entry, 2),
                            exit1_time_ist=to_ist(state_b["x1_time"]),
                            exit1_premium=x1_p,
                            exit2_time_ist=to_ist(t),
                            exit2_premium=x2_p,
                            avg_exit_premium=round(avg_exit, 2),
                            pnl_pct=round(pnl, 2),
                            bricks_held=state_b["bricks_held"],
                            holding_minutes=int((t - state_b["e1_time"]).total_seconds() // 60),
                        ))
                        state_b = None
                    elif b.direction == "up":
                        # Up after warning — re-arm: clear x1, return to "full"
                        state_b["x1_time"] = None
                        state_b["x1_premium"] = None
                        state_b["phase"] = "full"

        # ── Reports
        def summarize(trades, label):
            if not trades:
                return {"label": label, "n_trades": 0}
            pnls = [t.pnl_pct for t in trades]
            ce = [t for t in trades if t.side == "CE"]
            pe = [t for t in trades if t.side == "PE"]
            return {
                "label": label,
                "n_trades": len(trades),
                "win_rate_pct": round(100 * sum(1 for p in pnls if p > 0) / len(pnls), 2),
                "sum_pnl_pct": round(sum(pnls), 2),
                "avg_pnl_pct": round(mean(pnls), 2),
                "median_pnl_pct": round(median(pnls), 2),
                "best": round(max(pnls), 2),
                "worst": round(min(pnls), 2),
                "median_hold_min": int(median([t.holding_minutes for t in trades])),
                "CE": {
                    "n": len(ce),
                    "wr": round(100*sum(1 for t in ce if t.pnl_pct>0)/len(ce), 2) if ce else None,
                    "avg": round(mean(t.pnl_pct for t in ce), 2) if ce else None,
                    "sum": round(sum(t.pnl_pct for t in ce), 2) if ce else None,
                },
                "PE": {
                    "n": len(pe),
                    "wr": round(100*sum(1 for t in pe if t.pnl_pct>0)/len(pe), 2) if pe else None,
                    "avg": round(mean(t.pnl_pct for t in pe), 2) if pe else None,
                    "sum": round(sum(t.pnl_pct for t in pe), 2) if pe else None,
                },
            }

        # Per-month split
        def by_month(trades):
            buckets = {}
            for t in trades:
                m = t.day[:7]
                buckets.setdefault(m, []).append(t)
            return {m: summarize(trades_in, m) for m, trades_in in sorted(buckets.items())}

        sum_a = summarize(trades_a, "A: full-size sequential")
        sum_b = summarize(trades_b, "B: 50/50 scaling")
        by_month_a = by_month(trades_a)
        by_month_b = by_month(trades_b)

        print("\n=== VERSION A — all trades ===")
        for t in trades_a:
            print(f"  [{t.day}] {t.side} {t.strike} ({t.expiry})  in {t.entry_time_ist[11:16]} ₹{t.entry_premium:.1f} (spot {t.entry_spot:.0f}) -> out {t.exit_time_ist} ₹{t.exit_premium:.1f}  | {t.pnl_pct:+7.2f}%  block₹{t.block_size}  hold={t.holding_minutes}min")
        print(f"\n=== VERSION A summary ===\n{json.dumps(sum_a, indent=2)}")
        print(f"\n=== VERSION A by month ===\n{json.dumps(by_month_a, indent=2)}")

        print("\n=== VERSION B — all trades ===")
        for t in trades_b:
            print(f"  [{t.day}] {t.side} {t.strike}  e1 ₹{t.entry1_premium:.1f}  e2 ₹{t.entry2_premium:.1f}  → x1 ₹{t.exit1_premium:.1f}  x2 ₹{t.exit2_premium:.1f}  avg in ₹{t.avg_entry_premium:.1f} avg out ₹{t.avg_exit_premium:.1f}  | {t.pnl_pct:+7.2f}%  hold={t.holding_minutes}min")
        print(f"\n=== VERSION B summary ===\n{json.dumps(sum_b, indent=2)}")
        print(f"\n=== VERSION B by month ===\n{json.dumps(by_month_b, indent=2)}")

        # CSV outputs
        if trades_a:
            with (REPORT_DIR / "trades_A.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(asdict(trades_a[0]).keys())); w.writeheader()
                for t in trades_a: w.writerow(asdict(t))
        if trades_b:
            with (REPORT_DIR / "trades_B.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(asdict(trades_b[0]).keys())); w.writeheader()
                for t in trades_b: w.writerow(asdict(t))
        (REPORT_DIR / "summary.json").write_text(json.dumps({
            "A_overall": sum_a, "B_overall": sum_b,
            "A_by_month": by_month_a, "B_by_month": by_month_b,
            "data_note": "Jan-Feb 2026 NIFTY traded 25-26k; we lack strike data above 24,050 so those months are not tradable. Mar-May only.",
        }, indent=2))
        print(f"\nwrote: {REPORT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
