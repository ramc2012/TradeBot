"""
Strategy 2 Enhanced — Expansion Modules Integrated
====================================================

Adds two expansion-capture modules to the existing MACD + IBR + POC strategy:

MODULE A — PE Overnight Carry (Bearish Trend Continuation)
  Signal:  SENSEX TREND_DN day (IB broken down only, range > 2× IBR, close in bottom 30%)
  Entry:   Buy ATM PE at session close (last 5-min candle of trend day)
  Exit:    Sell PE at next session open (first 5-min candle next day) — "gap exit"
  Sizing:  10% allocation (conservative — overnight risk)
  Edge:    81.8% gap continuation rate after TREND_DN days

MODULE B — Intraday Expansion (Open-Drive / Open-Test-Drive)
  Signal:  Detect Open-Drive or Open-Test-Drive in first 15-30 minutes
           OD:  Open outside prev VA + first 3×5-min bars extend away
           OTD: Open inside prev VA + brief test then breaks out
  Entry:   At 09:45 (after 6th 5-min bar confirms direction)
  Exit:    IBR target (dynamic) or session close
  Sizing:  20% (base) or 35% when combined with POC reversion

Combined with Strategy 2 baseline:
  Strategy D★ = IBR target + POC alloc + PE overnight carry + intraday expansion

Output: runtime/index_analytics_data/expansion/
"""
from __future__ import annotations

import csv, gzip, json, math, os
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_ROOT   = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"
OUTPUT_ROOT = DATA_ROOT / "expansion"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

UNDERLYING   = "SENSEX"
FLOOR_PCT    = -50.0
ALLOC_BASE   = 0.20
ALLOC_HIGH   = 0.35
ALLOC_LOW    = 0.10
ALLOC_CARRY  = 0.10    # conservative for overnight
TARGET_PCT   = 30.0

# ── Data loading ──────────────────────────────────────────────────────────────
@lru_cache(maxsize=512)
def _load_1m(path_str: str) -> pd.DataFrame:
    path = DATA_ROOT / path_str
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

@lru_cache(maxsize=512)
def _resample5(path_str: str) -> pd.DataFrame:
    df = _load_1m(path_str)
    agg = {k: v for k, v in
           {"open": "first", "high": "max", "low": "min", "close": "last",
            "volume": "sum", "oi": "last"}.items() if k in df.columns}
    r = (df.set_index("time")
           .resample("5min", label="right", closed="right")
           .agg(agg).dropna(subset=["open", "close"]).reset_index())
    return r

@lru_cache(maxsize=4)
def _spot_df() -> pd.DataFrame:
    return _load_1m(f"spot/underlying={UNDERLYING}/1minute.csv.gz")

def _load_daily_mp() -> pd.DataFrame:
    path = DATA_ROOT / "market_profile" / "daily_mp_params.csv"
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("poc", "vah", "val", "var", "ibh", "ibl", "ibr",
              "session_high", "session_low", "open_price", "close_price"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    return df


# ── Series descriptors (reused from option_mp_analysis) ──────────────────────
@dataclass(frozen=True)
class Desc:
    series_id:   str
    underlying:  str
    expiry_kind: str
    expiry:      str
    strike:      float
    ce_path:     str
    pe_path:     str
    pair_start:  str

def _build_descs() -> list[Desc]:
    raw = json.loads((DATA_ROOT / "contract_index.json").read_text())
    metas = [m for m in raw.values()
             if m.get("file_path") and m.get("candle_count") and
             m.get("earliest_candle") and m.get("strike") is not None and
             m.get("option_type") and m.get("expiry_kind") == "weekly"
             and m.get("underlying") == UNDERLYING]

    by_group: dict = defaultdict(list)
    for m in metas:
        by_group[(m["underlying"], m["expiry_kind"], m["expiry"])].append(m)

    descs = []
    spot = _spot_df().set_index("time").sort_index()

    for (und, ek, exp), grp in sorted(by_group.items()):
        ce_map = {float(m["strike"]): m for m in grp if m["option_type"] == "CE"}
        pe_map = {float(m["strike"]): m for m in grp if m["option_type"] == "PE"}
        common = sorted(set(ce_map) & set(pe_map))
        if not common: continue
        candidates = []
        for st in common:
            ce = ce_map[st]; pe = pe_map[st]
            ps  = max(pd.Timestamp(ce["earliest_candle"]), pd.Timestamp(pe["earliest_candle"]))
            pe2 = min(pd.Timestamp(ce["latest_candle"]),  pd.Timestamp(pe["latest_candle"]))
            if pe2 > ps: candidates.append((st, ps, ce, pe))
        if not candidates: continue
        start_day = min(p for _, p, _, _ in candidates).date()
        first_ts  = min(p for _, p, _, _ in candidates)
        before    = spot.loc[:first_ts]
        if before.empty: continue
        sp = float(before.iloc[-1]["close"])
        eligible = [c for c in candidates if c[1].date() == start_day] or candidates
        strike, pair_start, ce_m, pe_m = min(eligible, key=lambda c: (abs(c[0] - sp), c[1], c[0]))
        descs.append(Desc(
            series_id=f"{und}|{ek}|{exp}",
            underlying=und, expiry_kind=ek, expiry=exp,
            strike=float(strike),
            ce_path=ce_m["file_path"], pe_path=pe_m["file_path"],
            pair_start=pair_start.isoformat(),
        ))
    return descs


# ── Day-type classification (from expansion_analysis) ────────────────────────
def _classify_day(r) -> str:
    """Classify a single MP row into day type."""
    session_range = r["session_high"] - r["session_low"]
    ibr = r["ibr"]
    if ibr <= 0 or session_range <= 0:
        return "UNKNOWN"
    range_ratio = session_range / ibr
    close_pos = (r["close_price"] - r["session_low"]) / session_range
    ib_up = r["ib_broken_up"]
    ib_dn = r["ib_broken_dn"]

    if (ib_up != ib_dn) and range_ratio >= 2.0:
        if ib_up and close_pos >= 0.70: return "TREND_UP"
        if ib_dn and close_pos <= 0.30: return "TREND_DN"
    if ib_up and ib_dn and range_ratio >= 1.5: return "DOUBLE_DIST"
    if (ib_up != ib_dn) and range_ratio >= 1.2:
        if ib_up: return "NORMAL_VAR_UP"
        return "NORMAL_VAR_DN"
    if r["fa_up"] or r["fa_dn"]: return "FAILED_AUCTION"
    return "NORMAL"


# ── MACD engine ──────────────────────────────────────────────────────────────
def _ema(values, period):
    n = len(values); result = [None] * n
    if n < period: return result
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    k = 2.0 / (period + 1); prev = sma
    for i in range(period, n):
        v = values[i] * k + prev * (1.0 - k); result[i] = v; prev = v
    return result

def _macd(closes):
    n = len(closes)
    ef = _ema(closes, 12); es = _ema(closes, 26)
    ml = [(ef[i] - es[i]) if ef[i] is not None and es[i] is not None else None for i in range(n)]
    fv = next((i for i, v in enumerate(ml) if v is not None), -1)
    sl = [None] * n
    if fv == -1: return ml, sl
    es2 = _ema([ml[i] for i in range(fv, n)], 9)
    for j, v in enumerate(es2): sl[fv + j] = v
    return ml, sl


# ── Trade simulation ─────────────────────────────────────────────────────────
def _sim_target(candles, entry_idx: int, target_pct: float) -> dict:
    ep = float(candles[entry_idx]["close"])
    tp = ep * (1.0 + target_pct / 100.0)
    for i in range(entry_idx + 1, len(candles)):
        if float(candles[i]["high"]) >= tp:
            return {"exit_idx": i, "blended_return": target_pct,
                    "exit_reason": "target_hit", "exit_time": str(candles[i]["time"])}
    i = len(candles) - 1
    cl = float(candles[i]["close"])
    return {"exit_idx": i,
            "blended_return": round((cl - ep) / ep * 100.0, 4),
            "exit_reason": "hold_to_end", "exit_time": str(candles[i]["time"])}

def _sim_ibr_target(candles, entry_idx: int, ibr_tgt_pct: float,
                    ibr_sl_pct: float) -> dict:
    """Exit at IBR-derived target, with IBR-derived stop loss."""
    ep = float(candles[entry_idx]["close"])
    tp = ep * (1.0 + ibr_tgt_pct / 100.0)
    sl = ep * (1.0 + ibr_sl_pct / 100.0)  # sl_pct is negative
    for i in range(entry_idx + 1, len(candles)):
        hi = float(candles[i]["high"])
        lo = float(candles[i]["low"])
        # Check SL first (worst case in same bar)
        if lo <= sl:
            return {"exit_idx": i,
                    "blended_return": round(ibr_sl_pct, 4),
                    "exit_reason": "hard_sl_ibr",
                    "exit_time": str(candles[i]["time"])}
        if hi >= tp:
            return {"exit_idx": i,
                    "blended_return": round(ibr_tgt_pct, 4),
                    "exit_reason": "ibr_target_hit",
                    "exit_time": str(candles[i]["time"])}
    i = len(candles) - 1
    cl = float(candles[i]["close"])
    return {"exit_idx": i,
            "blended_return": round((cl - ep) / ep * 100.0, 4),
            "exit_reason": "hold_to_end",
            "exit_time": str(candles[i]["time"])}

# Trail stop simulator for expansion intraday
def _sim_trail(candles, entry_idx: int, target_pct: float,
               trail_pct: float = 20.0) -> dict:
    """Layered exit: 50% at target, remaining trailed."""
    ep = float(candles[entry_idx]["close"])
    tp = ep * (1.0 + target_pct / 100.0)
    peak = ep
    target_hit = False

    for i in range(entry_idx + 1, len(candles)):
        hi = float(candles[i]["high"])
        lo = float(candles[i]["low"])
        cl = float(candles[i]["close"])

        peak = max(peak, hi)

        if not target_hit and hi >= tp:
            target_hit = True
            continue

        if target_hit:
            trail_stop = peak * (1.0 - trail_pct / 100.0)
            if lo <= trail_stop:
                # Blended: 50% at target + 50% at trail
                trail_ret = (trail_stop - ep) / ep * 100.0
                blended = 0.5 * target_pct + 0.5 * trail_ret
                return {"exit_idx": i, "blended_return": round(blended, 4),
                        "exit_reason": "trail_stop",
                        "exit_time": str(candles[i]["time"])}

    i = len(candles) - 1
    cl = float(candles[i]["close"])
    end_ret = (cl - ep) / ep * 100.0
    if target_hit:
        blended = 0.5 * target_pct + 0.5 * end_ret
    else:
        blended = end_ret
    return {"exit_idx": i, "blended_return": round(blended, 4),
            "exit_reason": "hold_to_end",
            "exit_time": str(candles[i]["time"])}


# ── Compounding ──────────────────────────────────────────────────────────────
def _compound_variable(rows, floor=FLOOR_PCT, start=100_000.0):
    eq = float(start)
    for row in rows:
        eq = eq + eq * row["alloc"] * max(row["blended_return"], floor) / 100.0
    return eq


# ── Option MP computation (simplified) ───────────────────────────────────────
def _compute_option_ib(day_df: pd.DataFrame) -> dict:
    """Compute IBH, IBL, IBR, IB_open from first 60 min of option 1-min data."""
    times = day_df["time"]
    session_start = times.iloc[0]
    ib_mask = times < session_start + pd.Timedelta(minutes=60)
    ib_df = day_df[ib_mask]
    if len(ib_df) < 30:
        return {}

    ibh = float(ib_df["high"].max())
    ibl = float(ib_df["low"].min())
    ibr = ibh - ibl
    ib_open = float(ib_df["open"].iloc[0])
    if ibr <= 0 or ib_open <= 0:
        return {}

    return {"ibh": ibh, "ibl": ibl, "ibr": ibr, "ib_open": ib_open,
            "ibr_tgt_pct": max((ibh + ibr - ib_open) / ib_open * 100.0, 15.0),
            "ibr_sl_pct": min((ibl - ibr - ib_open) / ib_open * 100.0, -5.0)}


# ═══════════════════════════════════════════════════════════════════════════════
#  MODULE A — PE OVERNIGHT CARRY
# ═══════════════════════════════════════════════════════════════════════════════

def run_pe_overnight_carry(descs: list[Desc], mp: pd.DataFrame) -> list[dict]:
    """
    Signal:  TREND_DN day detected at close
    Entry:   Buy ATM PE at last 5-min candle of the trend day
    Exit:    Sell PE at first 5-min candle of next trading day (gap capture)

    Returns list of trade dicts compatible with equity curve computation.
    """
    print("\n  MODULE A — PE Overnight Carry")
    print("  " + "-" * 50)

    # Enrich MP with day types and get TREND_DN dates
    mp = mp.copy()
    mp["day_type"] = mp.apply(_classify_day, axis=1)
    mp["prev_vah"] = mp["vah"].shift(1)
    mp["prev_val"] = mp["val"].shift(1)

    trend_dn_dates = set(mp[mp["day_type"] == "TREND_DN"]["date"].values)
    print(f"    TREND_DN days: {len(trend_dn_dates)}")

    # Build date → next trading date map
    sorted_dates = sorted(mp["date"].values)
    next_date_map = {}
    for i in range(len(sorted_dates) - 1):
        next_date_map[sorted_dates[i]] = sorted_dates[i + 1]

    # Map dates to active series (which expiry's ATM PE to use)
    date_to_desc = {}
    for desc in descs:
        try:
            pe5 = _resample5(desc.pe_path)
        except FileNotFoundError:
            continue
        pe5 = pe5[pe5["time"] >= pd.Timestamp(desc.pair_start)].copy()
        if len(pe5) < 10:
            continue
        for d in pe5["time"].dt.date.unique():
            if d not in date_to_desc:
                date_to_desc[d] = desc

    trades = []
    for signal_date in trend_dn_dates:
        if signal_date not in next_date_map:
            continue
        next_td = next_date_map[signal_date]

        desc = date_to_desc.get(signal_date)
        if desc is None:
            continue

        try:
            pe5 = _resample5(desc.pe_path)
        except FileNotFoundError:
            continue

        pe5 = pe5[pe5["time"] >= pd.Timestamp(desc.pair_start)].copy().reset_index(drop=True)

        # Get last candle of signal day
        signal_day_bars = pe5[pe5["time"].dt.date == signal_date]
        if len(signal_day_bars) == 0:
            continue
        entry_bar = signal_day_bars.iloc[-1]
        entry_price = float(entry_bar["close"])
        entry_time = entry_bar["time"]

        if entry_price <= 1.0:
            continue  # skip worthless options

        # Get first candle of next trading day
        next_day_bars = pe5[pe5["time"].dt.date == next_td]
        if len(next_day_bars) == 0:
            continue
        exit_bar = next_day_bars.iloc[0]
        exit_price = float(exit_bar["open"])  # exit at open
        exit_time = exit_bar["time"]

        ret = (exit_price - entry_price) / entry_price * 100.0

        # Get spot context
        mp_row = mp[mp["date"] == signal_date]
        spot_close = float(mp_row["close_price"].iloc[0]) if len(mp_row) > 0 else 0.0

        trades.append({
            "module": "A_pe_carry",
            "series_id": desc.series_id,
            "expiry": desc.expiry,
            "option_type": "PE",
            "signal_date": str(signal_date),
            "entry_time": str(entry_time),
            "entry_price": round(entry_price, 2),
            "exit_time": str(exit_time),
            "exit_price": round(exit_price, 2),
            "blended_return": round(ret, 4),
            "exit_reason": "gap_exit",
            "alloc": ALLOC_CARRY,
            "month": entry_time.strftime("%Y-%m"),
            "spot_at_signal": round(spot_close, 2),
        })

    wins = sum(1 for t in trades if t["blended_return"] > 0)
    wr = wins / len(trades) * 100 if trades else 0
    avg = np.mean([t["blended_return"] for t in trades]) if trades else 0
    med = np.median([t["blended_return"] for t in trades]) if trades else 0

    print(f"    Trades: {len(trades)}  WR: {wr:.1f}%  Avg: {avg:+.2f}%  Median: {med:+.2f}%")
    if trades:
        eq = _compound_variable(trades)
        print(f"    Equity (10% alloc): ₹{eq:,.0f}")

    return trades


# ═══════════════════════════════════════════════════════════════════════════════
#  MODULE B — INTRADAY EXPANSION (OPEN-DRIVE / OPEN-TEST-DRIVE)
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_opening_type_intraday(spot_day_1m: pd.DataFrame,
                                   prev_vah: float, prev_val: float,
                                   prev_poc: float) -> tuple[str, str]:
    """
    Detect opening type from first 30 minutes of 1-min spot data.
    Returns (opening_type, direction).

    Logic:
      - First bar: where does it open relative to prev VA?
      - First 6 bars (6 min): direction and momentum
      - First 15 bars (15 min): confirmation
      - First 30 bars (30 min): IB half confirmation

    OD (Open-Drive):
      Open outside prev VA, first 15 bars all extend further away
      No single bar reverses back into VA

    OTD (Open-Test-Drive):
      Open inside prev VA, first 5-10 bars test one direction,
      then remaining bars drive in opposite direction beyond VA edge
    """
    if len(spot_day_1m) < 30 or prev_vah <= 0 or prev_val <= 0:
        return "UNKNOWN", "NEUTRAL"

    first_open = float(spot_day_1m["open"].iloc[0])
    first_15 = spot_day_1m.iloc[:15]
    first_30 = spot_day_1m.iloc[:30]

    h15 = float(first_15["high"].max())
    l15 = float(first_15["low"].min())
    c15 = float(first_15["close"].iloc[-1])
    h30 = float(first_30["high"].max())
    l30 = float(first_30["low"].min())
    c30 = float(first_30["close"].iloc[-1])

    open_above_va = first_open > prev_vah
    open_below_va = first_open < prev_val
    open_inside = not open_above_va and not open_below_va

    # OD_UP: Open above prev VAH, first 30 min high keeps extending up
    # Never touches prev VAH (stays above)
    if open_above_va and l30 > prev_vah and c30 > first_open:
        return "OD_UP", "BULLISH"

    # OD_DN: Open below prev VAL, stays below
    if open_below_va and h30 < prev_val and c30 < first_open:
        return "OD_DN", "BEARISH"

    # OTD_UP: Open inside VA, tests down briefly, then drives above VAH
    if open_inside and h30 > prev_vah and c30 > prev_vah:
        # Check that it tested lower first (first 10 min had lower low)
        first_10 = spot_day_1m.iloc[:10]
        tested_low = float(first_10["low"].min()) < first_open - (prev_vah - prev_val) * 0.1
        if tested_low or c30 > prev_vah + (prev_vah - prev_val) * 0.2:
            return "OTD_UP", "BULLISH"

    # OTD_DN: Open inside VA, tests up briefly, then drives below VAL
    if open_inside and l30 < prev_val and c30 < prev_val:
        first_10 = spot_day_1m.iloc[:10]
        tested_high = float(first_10["high"].max()) > first_open + (prev_vah - prev_val) * 0.1
        if tested_high or c30 < prev_val - (prev_vah - prev_val) * 0.2:
            return "OTD_DN", "BEARISH"

    # Open-Auction or non-directional
    if open_inside:
        return "OA", "NEUTRAL"

    # Open outside but didn't extend (potential ORR)
    if open_above_va and c30 < prev_vah:
        return "ORR_DN", "BEARISH"
    if open_below_va and c30 > prev_val:
        return "ORR_UP", "BULLISH"

    return "OTHER", "NEUTRAL"


def run_intraday_expansion(descs: list[Desc], mp: pd.DataFrame) -> list[dict]:
    """
    Detect OD/OTD openings using 1-min spot data, trade with options.

    Entry at 09:45 (30 min after open, after OD/OTD confirmed)
    - OD_UP / OTD_UP: Buy CE
    - OD_DN / OTD_DN: Buy PE

    Exit: IBR target (if available) or 30% target or session close
    """
    print("\n  MODULE B — Intraday Expansion (OD/OTD)")
    print("  " + "-" * 50)

    # Load full 1-min spot data
    spot_1m = _spot_df()
    spot_1m_by_date = {d: g for d, g in spot_1m.groupby(spot_1m["time"].dt.date)}

    # Build prev VA from MP data
    mp = mp.copy()
    mp["prev_vah"] = mp["vah"].shift(1)
    mp["prev_val"] = mp["val"].shift(1)
    mp["prev_poc"] = mp["poc"].shift(1)
    mp_dict = {row["date"]: row for _, row in mp.iterrows()}

    # Map dates to active series
    date_to_desc = {}
    for desc in descs:
        for opt_type, path in [("CE", desc.ce_path), ("PE", desc.pe_path)]:
            try:
                f5 = _resample5(path)
            except FileNotFoundError:
                continue
            f5 = f5[f5["time"] >= pd.Timestamp(desc.pair_start)].copy()
            for d in f5["time"].dt.date.unique():
                key = (d, opt_type)
                if key not in date_to_desc:
                    date_to_desc[key] = (desc, path)

    trades = []
    opening_stats = defaultdict(int)

    for date_val, spot_day in sorted(spot_1m_by_date.items()):
        mp_row = mp_dict.get(date_val)
        if mp_row is None:
            continue
        prev_vah = mp_row.get("prev_vah")
        prev_val = mp_row.get("prev_val")
        prev_poc = mp_row.get("prev_poc")
        if pd.isna(prev_vah) or pd.isna(prev_val) or prev_vah <= 0:
            continue

        # Detect opening type from 1-min spot
        opening_type, direction = _detect_opening_type_intraday(
            spot_day, prev_vah, prev_val, prev_poc
        )
        opening_stats[opening_type] += 1

        if opening_type not in ("OD_UP", "OD_DN", "OTD_UP", "OTD_DN"):
            continue

        # Determine which option to buy
        if direction == "BULLISH":
            opt_type = "CE"
        else:
            opt_type = "PE"

        key = (date_val, opt_type)
        if key not in date_to_desc:
            continue
        desc, path = date_to_desc[key]

        try:
            f5 = _resample5(path)
        except FileNotFoundError:
            continue
        f5 = f5[f5["time"] >= pd.Timestamp(desc.pair_start)].copy().reset_index(drop=True)

        # Find entry candle at ~09:45 (6th 5-min bar: 09:20, 09:25, 09:30, 09:35, 09:40, 09:45)
        day_bars = f5[f5["time"].dt.date == date_val].copy().reset_index(drop=True)
        if len(day_bars) < 10:
            continue

        # Entry at 6th bar (index 5, approx 09:45)
        entry_idx_local = min(5, len(day_bars) - 2)
        entry_bar = day_bars.iloc[entry_idx_local]
        entry_price = float(entry_bar["close"])
        entry_time = entry_bar["time"]

        if entry_price <= 1.0:
            continue

        # Compute option IB for target
        try:
            opt_1m = _load_1m(path)
            opt_day_1m = opt_1m[opt_1m["time"].dt.date == date_val]
            ib_data = _compute_option_ib(opt_day_1m)
        except Exception:
            ib_data = {}

        if ib_data:
            ibr_tgt = ib_data["ibr_tgt_pct"]
        else:
            ibr_tgt = TARGET_PCT

        # Check POC reversion for allocation
        spot_at_entry = float(spot_day["close"].iloc[min(30, len(spot_day) - 1)])
        spot_poc = mp_row["poc"]
        if opt_type == "CE":
            poc_rev = spot_at_entry < spot_poc if spot_poc > 0 else False
        else:
            poc_rev = spot_at_entry > spot_poc if spot_poc > 0 else False

        alloc = ALLOC_HIGH if poc_rev else ALLOC_BASE

        # Simulate trade on remaining candles of the day
        candles = day_bars.to_dict("records")
        res = _sim_trail(candles, entry_idx_local, min(ibr_tgt, 80.0), trail_pct=15.0)

        trades.append({
            "module": "B_intraday_expansion",
            "series_id": desc.series_id,
            "expiry": desc.expiry,
            "option_type": opt_type,
            "signal_date": str(date_val),
            "opening_type": opening_type,
            "entry_time": str(entry_time),
            "entry_price": round(entry_price, 2),
            "exit_time": res["exit_time"],
            "exit_reason": res["exit_reason"],
            "blended_return": res["blended_return"],
            "alloc": alloc,
            "month": entry_time.strftime("%Y-%m"),
            "spot_at_signal": round(spot_at_entry, 2),
            "ibr_target": round(ibr_tgt, 1),
            "poc_reversion": poc_rev,
        })

    print(f"\n    Opening type detection stats:")
    for ot, cnt in sorted(opening_stats.items(), key=lambda x: -x[1]):
        print(f"      {ot:<12} {cnt:>4}")

    wins = sum(1 for t in trades if t["blended_return"] > 0)
    wr = wins / len(trades) * 100 if trades else 0
    avg = np.mean([t["blended_return"] for t in trades]) if trades else 0
    med = np.median([t["blended_return"] for t in trades]) if trades else 0

    print(f"\n    Trades: {len(trades)}  WR: {wr:.1f}%  Avg: {avg:+.2f}%  Median: {med:+.2f}%")

    # Breakdown by opening type
    for ot in ["OD_UP", "OD_DN", "OTD_UP", "OTD_DN"]:
        sub = [t for t in trades if t.get("opening_type") == ot]
        if not sub: continue
        sub_wr = sum(1 for t in sub if t["blended_return"] > 0) / len(sub) * 100
        sub_avg = np.mean([t["blended_return"] for t in sub])
        print(f"      {ot:<8} n={len(sub):>3}  WR={sub_wr:.1f}%  Avg={sub_avg:+.2f}%")

    if trades:
        eq = _compound_variable(trades)
        print(f"    Equity: ₹{eq:,.0f}")

    return trades


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY 2 BASELINE — MACD + IBR + POC (from final_strategy_trades.csv)
# ═══════════════════════════════════════════════════════════════════════════════

def load_strategy2_baseline() -> list[dict]:
    """
    Load the existing Strategy 2 D★ trades.

    D★ = target_50pct + POC allocation (35/10%) + floor at -50%.
    Source: staggered_exit/trade_results.csv (target_50pct for SENSEX)
    + option_mp/final_strategy_trades.csv (for poc_alloc mapping).

    Original result: ~₹84L from ₹1L over 11 months.
    """
    # 1. Load target_50pct trades from trade_results.csv
    tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
    if not tr_path.exists():
        print("    WARNING: staggered_exit/trade_results.csv not found")
        return []

    df_tr = pd.read_csv(tr_path)
    sensex_50 = df_tr[
        (df_tr["underlying"] == UNDERLYING) & (df_tr["strategy"] == "target_50pct")
    ].sort_values("entry_time").copy()

    # 2. Load POC alloc mapping from final_strategy_trades.csv
    poc_path = DATA_ROOT / "option_mp" / "final_strategy_trades.csv"
    poc_lookup = {}
    if poc_path.exists():
        df_poc = pd.read_csv(poc_path)
        for _, row in df_poc.iterrows():
            poc_lookup[row["entry_time"]] = row["poc_alloc"]

    trades = []
    for _, row in sensex_50.iterrows():
        entry_ts = pd.Timestamp(row["entry_time"])
        ret = row["blended_return"]
        alloc = poc_lookup.get(row["entry_time"], ALLOC_BASE)

        trades.append({
            "module": "S2_baseline",
            "series_id": f"SENSEX|weekly|{row['expiry']}",
            "expiry": row["expiry"],
            "option_type": row["option_type"],
            "signal_date": str(entry_ts.date()),
            "entry_time": str(row["entry_time"]),
            "entry_price": row["entry_price"],
            "blended_return": ret,
            "exit_reason": row["exit_reason"],
            "alloc": alloc,
            "month": entry_ts.strftime("%Y-%m"),
        })
    return trades


# ═══════════════════════════════════════════════════════════════════════════════
#  COMBINED EQUITY CURVE
# ═══════════════════════════════════════════════════════════════════════════════

def compute_combined_equity(s2_trades, carry_trades, expansion_trades):
    """
    Merge all three trade streams, sort by entry time, compute combined equity.
    """
    all_trades = []
    for t in s2_trades:
        all_trades.append({**t, "source": "S2_baseline"})
    for t in carry_trades:
        all_trades.append({**t, "source": "A_pe_carry"})
    for t in expansion_trades:
        all_trades.append({**t, "source": "B_expansion"})

    # Sort by entry time
    all_trades.sort(key=lambda t: t.get("entry_time", ""))

    # Compute equity curves
    strategies = {
        "S2 Only (D★ baseline)": [t for t in all_trades if t["source"] == "S2_baseline"],
        "S2 + PE Carry": [t for t in all_trades if t["source"] in ("S2_baseline", "A_pe_carry")],
        "S2 + Expansion": [t for t in all_trades if t["source"] in ("S2_baseline", "B_expansion")],
        "S2 + Both (Enhanced D★)": all_trades,
    }

    print("\n" + "=" * 90)
    print(f"  {'Strategy':<30} {'Trades':>7} {'WR%':>7} {'Avg%':>8} {'FinalEq':>12} "
          f"{'vs S2':>8} {'PosM':>6}")
    print("=" * 90)

    results = {}
    s2_eq = None

    for name, trades in strategies.items():
        if not trades:
            continue
        trades_sorted = sorted(trades, key=lambda t: t.get("entry_time", ""))
        n = len(trades_sorted)
        rets = [t["blended_return"] for t in trades_sorted]
        wr = sum(1 for r in rets if r > 0) / n * 100
        avg = np.mean(rets)

        eq = _compound_variable(trades_sorted)

        months = defaultdict(list)
        for t in trades_sorted:
            months[t.get("month", "")].append(t["blended_return"])
        pos_m = sum(1 for v in months.values() if sum(v) > 0)
        tot_m = len(months)

        if s2_eq is None:
            s2_eq = eq
        vs_s2 = f"{(eq - s2_eq) / s2_eq * 100:+.1f}%" if s2_eq > 0 else "—"

        print(f"  {name:<30} {n:>7} {wr:>6.1f}% {avg:>+7.2f} {eq/1e5:>10.3f}L "
              f"{vs_s2:>8} {pos_m:>2}/{tot_m}")

        results[name] = {
            "trades": trades_sorted, "eq": eq, "n": n, "wr": wr, "avg": avg,
            "pos_m": pos_m, "tot_m": tot_m,
        }

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  MONTHLY BREAKDOWN
# ═══════════════════════════════════════════════════════════════════════════════

def print_monthly_breakdown(results: dict):
    print("\n" + "=" * 90)
    print("  MONTHLY BREAKDOWN — COMBINED vs S2 ONLY")
    print("=" * 90)

    strat_keys = ["S2 Only (D★ baseline)", "S2 + Both (Enhanced D★)"]
    available = [k for k in strat_keys if k in results]
    if not available:
        return

    all_months = sorted(set(
        t.get("month", "") for k in available for t in results[k]["trades"]
    ))

    print(f"\n  {'Month':<9}", end="")
    for k in available:
        print(f"  {k[:28]:>28}", end="")
    print()
    print("  " + "-" * (9 + 30 * len(available)))

    for m in all_months:
        print(f"  {m:<9}", end="")
        for k in available:
            mtrades = [t for t in results[k]["trades"] if t.get("month") == m]
            n = len(mtrades)
            if n == 0:
                print(f"  {'—':>28}", end="")
                continue
            avg = np.mean([t["blended_return"] for t in mtrades])
            wr = sum(1 for t in mtrades if t["blended_return"] > 0) / n * 100
            # Per-month equity contribution
            eq_contrib = sum(t["alloc"] * max(t["blended_return"], FLOOR_PCT) / 100.0
                             for t in mtrades) * 100
            print(f"  {n:>3}tr {wr:>4.0f}%WR {avg:>+6.1f}% eq{eq_contrib:>+5.1f}%", end="")
        print()


# ═══════════════════════════════════════════════════════════════════════════════
#  VISUALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def plot_combined_dashboard(results: dict):
    fig = plt.figure(figsize=(22, 18))
    fig.suptitle("Strategy 2 Enhanced — Expansion Modules Integrated\nSENSEX Weekly Options",
                 fontsize=14, fontweight="bold")
    gs = gridspec.GridSpec(3, 3, hspace=0.40, wspace=0.30)

    # 1. Equity bars
    ax1 = fig.add_subplot(gs[0, 0])
    names = list(results.keys())
    eqs = [results[k]["eq"] / 1e5 for k in names]
    colors = ["#3498db", "#2ecc71", "#e67e22", "#e74c3c"][:len(names)]
    bars = ax1.barh(range(len(names)), eqs, color=colors, alpha=0.85)
    ax1.set_yticks(range(len(names)))
    ax1.set_yticklabels([n[:25] for n in names], fontsize=8)
    for bar, eq, name in zip(bars, eqs, names):
        n = results[name]["n"]
        wr = results[name]["wr"]
        ax1.text(eq + 0.05, bar.get_y() + bar.get_height() / 2,
                 f"₹{eq:.2f}L n={n} WR={wr:.0f}%", va="center", fontsize=7)
    ax1.set_xlabel("Final Equity (₹ Lakhs)")
    ax1.set_title("Strategy Comparison", fontweight="bold")

    # 2. Equity curves
    ax2 = fig.add_subplot(gs[0, 1:])
    for i, (name, data) in enumerate(results.items()):
        trades = sorted(data["trades"], key=lambda t: t.get("entry_time", ""))
        eq = 100_000.0
        curve = [eq]
        for t in trades:
            eq = eq + eq * t["alloc"] * max(t["blended_return"], FLOOR_PCT) / 100.0
            curve.append(eq)
        ax2.plot(curve, color=colors[i], linewidth=2, label=f"{name[:25]} ₹{curve[-1]/1e5:.2f}L")
    ax2.axhline(100_000, color="gray", ls="--", alpha=0.3)
    ax2.set_title("Equity Curves", fontweight="bold")
    ax2.set_ylabel("Equity (₹)")
    ax2.set_xlabel("Trade #")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x/1e5:.0f}L"))
    ax2.legend(fontsize=7, loc="upper left")
    ax2.grid(True, alpha=0.3)

    # 3. Module A — PE carry trade returns
    if "S2 + Both (Enhanced D★)" in results:
        carry_trades = [t for t in results["S2 + Both (Enhanced D★)"]["trades"]
                        if t.get("source") == "A_pe_carry"]
        ax3 = fig.add_subplot(gs[1, 0])
        if carry_trades:
            rets = [t["blended_return"] for t in carry_trades]
            ax3.bar(range(len(rets)), rets,
                    color=["#2ecc71" if r > 0 else "#e74c3c" for r in rets], alpha=0.7)
            ax3.axhline(0, color="black", ls="-", alpha=0.3)
            ax3.set_title(f"Module A: PE Carry Returns (n={len(rets)})", fontweight="bold", fontsize=9)
            ax3.set_xlabel("Trade #")
            ax3.set_ylabel("Return %")
        else:
            ax3.text(0.5, 0.5, "No carry trades", ha="center", va="center", transform=ax3.transAxes)

    # 4. Module B — Expansion trade returns
    expansion_trades = [t for t in results.get("S2 + Both (Enhanced D★)", {}).get("trades", [])
                        if t.get("source") == "B_expansion"]
    ax4 = fig.add_subplot(gs[1, 1])
    if expansion_trades:
        rets = [t["blended_return"] for t in expansion_trades]
        ax4.bar(range(len(rets)), rets,
                color=["#2ecc71" if r > 0 else "#e74c3c" for r in rets], alpha=0.7)
        ax4.axhline(0, color="black", ls="-", alpha=0.3)
        ax4.set_title(f"Module B: Expansion Returns (n={len(rets)})", fontweight="bold", fontsize=9)
        ax4.set_xlabel("Trade #")
        ax4.set_ylabel("Return %")
    else:
        ax4.text(0.5, 0.5, "No expansion trades", ha="center", va="center", transform=ax4.transAxes)

    # 5. Module B — by opening type
    ax5 = fig.add_subplot(gs[1, 2])
    if expansion_trades:
        ot_stats = defaultdict(list)
        for t in expansion_trades:
            ot_stats[t.get("opening_type", "?")].append(t["blended_return"])
        ot_names = sorted(ot_stats.keys())
        ot_avgs = [np.mean(ot_stats[k]) for k in ot_names]
        ot_wrs = [sum(1 for r in ot_stats[k] if r > 0) / len(ot_stats[k]) * 100 for k in ot_names]
        ot_colors = {"OD_UP": "#2ecc71", "OD_DN": "#e74c3c",
                     "OTD_UP": "#27ae60", "OTD_DN": "#c0392b"}
        ax5.bar(ot_names, ot_avgs,
                color=[ot_colors.get(k, "#999") for k in ot_names], alpha=0.85)
        for i, (name, avg, wr) in enumerate(zip(ot_names, ot_avgs, ot_wrs)):
            ax5.text(i, avg + 0.5, f"n={len(ot_stats[name])}\nWR={wr:.0f}%",
                     ha="center", fontsize=7)
        ax5.set_title("Expansion by Opening Type", fontweight="bold", fontsize=9)
        ax5.set_ylabel("Avg Return %")
    else:
        ax5.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax5.transAxes)

    # 6. Source breakdown for Enhanced D★
    ax6 = fig.add_subplot(gs[2, 0])
    if "S2 + Both (Enhanced D★)" in results:
        all_t = results["S2 + Both (Enhanced D★)"]["trades"]
        sources = ["S2_baseline", "A_pe_carry", "B_expansion"]
        source_labels = ["S2 MACD+IBR+POC", "PE Overnight Carry", "Intraday Expansion"]
        source_counts = [sum(1 for t in all_t if t.get("source") == s) for s in sources]
        source_wr = []
        for s in sources:
            st = [t for t in all_t if t.get("source") == s]
            source_wr.append(sum(1 for t in st if t["blended_return"] > 0) / len(st) * 100 if st else 0)
        ax6.barh(source_labels, source_counts, color=["#3498db", "#2ecc71", "#e67e22"], alpha=0.85)
        for i, (cnt, wr) in enumerate(zip(source_counts, source_wr)):
            ax6.text(cnt + 1, i, f"n={cnt} WR={wr:.0f}%", va="center", fontsize=8)
        ax6.set_title("Trade Source Breakdown", fontweight="bold", fontsize=9)

    # 7. Win rate comparison
    ax7 = fig.add_subplot(gs[2, 1])
    wr_names = list(results.keys())
    wr_vals = [results[k]["wr"] for k in wr_names]
    ax7.bar([n[:20] for n in wr_names], wr_vals,
            color=colors[:len(wr_names)], alpha=0.85)
    for i, (n, wr) in enumerate(zip(wr_names, wr_vals)):
        ax7.text(i, wr + 0.5, f"{wr:.1f}%", ha="center", fontsize=9)
    ax7.set_title("Win Rate Comparison", fontweight="bold", fontsize=9)
    ax7.set_ylabel("Win Rate %")
    ax7.tick_params(axis="x", labelsize=7, rotation=15)

    # 8. Monthly P&L heatmap (simplified)
    ax8 = fig.add_subplot(gs[2, 2])
    if "S2 + Both (Enhanced D★)" in results:
        all_t = results["S2 + Both (Enhanced D★)"]["trades"]
        months = defaultdict(float)
        for t in all_t:
            m = t.get("month", "?")
            months[m] += t["alloc"] * max(t["blended_return"], FLOOR_PCT) / 100.0 * 100
        ms = sorted(months.keys())
        vals = [months[m] for m in ms]
        ax8.bar(ms, vals, color=["#2ecc71" if v > 0 else "#e74c3c" for v in vals], alpha=0.85)
        ax8.set_title("Enhanced D★ Monthly Equity Change %", fontweight="bold", fontsize=9)
        ax8.set_ylabel("Equity Change %")
        ax8.tick_params(axis="x", labelsize=7, rotation=45)
    else:
        ax8.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax8.transAxes)

    plt.savefig(OUTPUT_ROOT / "combined_strategy_dashboard.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\n  Dashboard saved: {OUTPUT_ROOT / 'combined_strategy_dashboard.png'}")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    print("=" * 72)
    print("  STRATEGY 2 ENHANCED — EXPANSION MODULES")
    print("  SENSEX Weekly Options | Apr'25 — Apr'26")
    print("=" * 72)

    # 1. Load data
    print("\n[1] Loading data …")
    descs = _build_descs()
    mp = _load_daily_mp()
    print(f"    {len(descs)} weekly series, {len(mp)} trading days")

    # 2. Load Strategy 2 baseline trades
    print("\n[2] Loading Strategy 2 baseline (D★) …")
    s2_trades = load_strategy2_baseline()
    if s2_trades:
        eq = _compound_variable(s2_trades)
        wr = sum(1 for t in s2_trades if t["blended_return"] > 0) / len(s2_trades) * 100
        print(f"    S2 D★ baseline: {len(s2_trades)} trades, WR={wr:.1f}%, Equity=₹{eq:,.0f}")
    else:
        print("    No S2 baseline trades found. Running modules standalone.")

    # 3. Module A — PE Overnight Carry
    print("\n[3] Running Module A — PE Overnight Carry …")
    carry_trades = run_pe_overnight_carry(descs, mp)

    # 4. Module B — Intraday Expansion
    print("\n[4] Running Module B — Intraday Expansion …")
    expansion_trades = run_intraday_expansion(descs, mp)

    # 5. Combined equity
    print("\n[5] Computing combined equity curves …")
    results = compute_combined_equity(s2_trades, carry_trades, expansion_trades)

    # 6. Monthly breakdown
    print_monthly_breakdown(results)

    # 7. Save trade CSVs
    print("\n[6] Saving results …")
    all_exports = []
    for t in carry_trades:
        all_exports.append(t)
    for t in expansion_trades:
        all_exports.append(t)

    if all_exports:
        # Determine common keys
        keys = list(all_exports[0].keys())
        all_keys = set()
        for t in all_exports:
            all_keys.update(t.keys())
        all_keys = sorted(all_keys)

        csv_path = OUTPUT_ROOT / "expansion_module_trades.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_exports)
        print(f"    Expansion trades: {csv_path}")

    # 8. Dashboard
    print("\n[7] Generating dashboard …")
    plot_combined_dashboard(results)

    print("\n" + "=" * 72)
    print("  COMPLETE")
    print("=" * 72)

    return results


if __name__ == "__main__":
    run()
