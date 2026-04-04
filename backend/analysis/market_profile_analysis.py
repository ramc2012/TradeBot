"""
Market Profile Analysis — SENSEX Weekly Series
===============================================

Computes Market Profile parameters from SENSEX 1-min spot data and tests
MP-based strategies on MACD zero-cross ATM option signals.

Market Profile parameters computed:
  IB      — Initial Balance (first 60 min: 9:15–10:15)
  IBH/IBL — IB High / IB Low
  IBR     — IB Range = IBH − IBL
  POC     — Point of Control (price bucket with most TPOs)
  VA      — Value Area (70 % of TPO volume around POC)
  VAH/VAL — Value Area High / Low
  VAR     — Value Area Range = VAH − VAL
  FA      — Failed Auction flag (IB extension that closes back inside IB)
  PPOC    — Previous session's POC
  PVAL/PVAH — Previous session's VAL / VAH

TPO definition:
  Each 30-min half-hour block = one TPO period (13 periods per session)
  50-point price buckets for SENSEX (~75 000–86 000)

MP Strategies tested (applied to MACD zero-cross CE + PE option signals):
  0. baseline_both      — original: all MACD signals, both CE + PE
  1. mp_poc_filter      — MACD CE only if spot > daily POC, PE if < POC
  2. mp_ppoc_filter     — CE if spot > PPOC, PE if spot < PPOC
  3. mp_ib_breakout     — signal only when spot has already broken IB in signal direction
  4. mp_va_outside      — only enter when spot is OUTSIDE value area (breakout trades)
  5. mp_va_inside       — only enter when spot is INSIDE value area (mean-reversion)
  6. mp_ppoc_target     — enter all MACD signals; exit option at PPOC-implied % gain
  7. mp_fa_reversal     — trade only after a Failed Auction (FA) confirmation
  8. mp_combo_best      — POC filter + outside VA + IB break in same direction
  9. mp_poc_target      — all entries; exit at POC-to-price implied option move

Output: runtime/index_analytics_data/market_profile/
"""
from __future__ import annotations

import csv, gzip, json, math, os, sys
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

# ── Matplotlib (non-interactive) ───────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import FancyBboxPatch

# ── Paths ──────────────────────────────────────────────────────────────────────
DATA_ROOT   = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"
OUTPUT_ROOT = DATA_ROOT / "market_profile"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

UNDERLYING   = "SENSEX"
BUCKET_SIZE  = 50          # 50-pt price buckets for SENSEX TPOs
TPO_MINUTES  = 30          # each TPO period = 30 min
IB_MINUTES   = 60          # Initial Balance = first 60 min
VA_PCT       = 0.70        # Value Area = 70 % of TPOs
SESSION_START = "09:15"
SESSION_END   = "15:30"
MIN_TPO_PERIODS = 4        # skip sessions with <4 TPO periods

# ── Helpers ────────────────────────────────────────────────────────────────────
def _bucket(price: float) -> float:
    """Round price DOWN to nearest BUCKET_SIZE grid."""
    return math.floor(price / BUCKET_SIZE) * BUCKET_SIZE


# ── Spot data ──────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _load_spot() -> pd.DataFrame:
    path = DATA_ROOT / f"spot/underlying={UNDERLYING}/1minute.csv.gz"
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ── Market Profile engine ──────────────────────────────────────────────────────
@dataclass
class DailyMP:
    date:       str
    tpo_counts: dict        # bucket → TPO count
    poc:        float       # price bucket with max TPOs
    vah:        float
    val:        float
    var:        float       # VAH − VAL
    ibh:        float       # IB High
    ibl:        float       # IB Low
    ibr:        float       # IB Range
    ib_broken_up:   bool    # spot broke above IBH during session
    ib_broken_dn:   bool    # spot broke below IBL during session
    fa_up:      bool        # Failed Auction up (broke IBH then closed inside IB)
    fa_dn:      bool        # Failed Auction down (broke IBL then closed inside IB)
    session_high: float
    session_low:  float
    open_price:   float
    close_price:  float
    total_tpos:   int


def _compute_daily_mp(day_df: pd.DataFrame) -> Optional[DailyMP]:
    """Compute Market Profile from 1-min SENSEX spot data for one trading day."""
    day_df = day_df.copy().reset_index(drop=True)
    if len(day_df) < MIN_TPO_PERIODS * TPO_MINUTES:
        return None

    times = day_df["time"]
    date_str = str(times.iloc[0].date())

    # ── Initial Balance ─────────────────────────────────────────────────────
    ib_mask = (times >= times.iloc[0]) & (times < times.iloc[0] + pd.Timedelta(minutes=IB_MINUTES))
    ib_df = day_df[ib_mask]
    if ib_df.empty:
        return None
    ibh = float(ib_df["high"].max())
    ibl = float(ib_df["low"].min())
    ibr = ibh - ibl

    # ── TPO Counts ──────────────────────────────────────────────────────────
    # Resample 1-min candles into 30-min TPO periods; each period votes for
    # all price buckets between its low and high
    tpo_counts: dict[float, int] = defaultdict(int)
    tpo_periods = day_df.set_index("time").resample(f"{TPO_MINUTES}min",
                  label="left", closed="left").agg({"high": "max", "low": "min"}).dropna()

    for _, row in tpo_periods.iterrows():
        lo_b = _bucket(float(row["low"]))
        hi_b = _bucket(float(row["high"]))
        b = lo_b
        while b <= hi_b:
            tpo_counts[b] += 1
            b += BUCKET_SIZE

    if not tpo_counts:
        return None

    total_tpos = sum(tpo_counts.values())

    # ── POC ─────────────────────────────────────────────────────────────────
    poc = max(tpo_counts, key=lambda b: tpo_counts[b])

    # ── Value Area (70 %) ────────────────────────────────────────────────────
    sorted_buckets = sorted(tpo_counts.keys())
    poc_idx = sorted_buckets.index(poc)
    included = {poc}
    included_vol = tpo_counts[poc]
    target = math.ceil(total_tpos * VA_PCT)
    lo_ptr = poc_idx - 1
    hi_ptr = poc_idx + 1

    while included_vol < target:
        add_lo = tpo_counts.get(sorted_buckets[lo_ptr], 0) if lo_ptr >= 0 else 0
        add_hi = tpo_counts.get(sorted_buckets[hi_ptr], 0) if hi_ptr < len(sorted_buckets) else 0
        if add_lo == 0 and add_hi == 0:
            break
        # Add the side with more TPOs (ties go to high side per classic MP rule)
        if add_hi >= add_lo:
            included.add(sorted_buckets[hi_ptr])
            included_vol += add_hi
            hi_ptr += 1
        else:
            included.add(sorted_buckets[lo_ptr])
            included_vol += add_lo
            lo_ptr -= 1

    vah = max(included) + BUCKET_SIZE  # top of the highest included bucket
    val = min(included)                # bottom of the lowest included bucket
    var = vah - val

    # ── IB extension & Failed Auction ───────────────────────────────────────
    # Look at candles AFTER IB period
    post_ib = day_df[~ib_mask]
    session_high = float(day_df["high"].max())
    session_low  = float(day_df["low"].min())
    ib_broken_up = session_high > ibh
    ib_broken_dn = session_low  < ibl

    # FA = price extended beyond IB but the LAST candle of session closed inside IB
    last_close = float(day_df["close"].iloc[-1])
    fa_up = ib_broken_up and (last_close < ibh)
    fa_dn = ib_broken_dn and (last_close > ibl)

    return DailyMP(
        date=date_str,
        tpo_counts=dict(tpo_counts),
        poc=float(poc),
        vah=float(vah),
        val=float(val),
        var=float(var),
        ibh=float(ibh),
        ibl=float(ibl),
        ibr=float(ibr),
        ib_broken_up=ib_broken_up,
        ib_broken_dn=ib_broken_dn,
        fa_up=fa_up,
        fa_dn=fa_dn,
        session_high=session_high,
        session_low=session_low,
        open_price=float(day_df["open"].iloc[0]),
        close_price=last_close,
        total_tpos=total_tpos,
    )


# ── Weekly aggregate MP ────────────────────────────────────────────────────────
@dataclass
class WeeklyMP:
    series_id:    str
    expiry:       str
    daily_mps:    list[DailyMP]       # ordered Mon→Fri
    weekly_poc:   float               # POC across all TPOs of the week
    weekly_vah:   float
    weekly_val:   float
    weekly_ibh:   float               # Day-1 IB High
    weekly_ibl:   float               # Day-1 IB Low
    weekly_ibr:   float
    ppoc:         float               # Previous series weekly POC
    pvah:         float               # Previous series weekly VAH
    pval:         float               # Previous series weekly VAL


def _compute_weekly_mp(days: list[DailyMP]) -> tuple[float, float, float]:
    """Return (weekly_poc, weekly_vah, weekly_val) from list of DailyMP."""
    merged: dict[float, int] = defaultdict(int)
    for d in days:
        for b, cnt in d.tpo_counts.items():
            merged[b] += cnt
    if not merged:
        return 0, 0, 0
    total = sum(merged.values())
    poc = max(merged, key=lambda b: merged[b])
    sorted_b = sorted(merged.keys())
    poc_idx = sorted_b.index(poc)
    included = {poc}
    inc_vol = merged[poc]
    target = math.ceil(total * VA_PCT)
    lo_ptr = poc_idx - 1; hi_ptr = poc_idx + 1
    while inc_vol < target:
        add_lo = merged.get(sorted_b[lo_ptr], 0) if lo_ptr >= 0 else 0
        add_hi = merged.get(sorted_b[hi_ptr], 0) if hi_ptr < len(sorted_b) else 0
        if add_lo == 0 and add_hi == 0: break
        if add_hi >= add_lo:
            included.add(sorted_b[hi_ptr]); inc_vol += add_hi; hi_ptr += 1
        else:
            included.add(sorted_b[lo_ptr]); inc_vol += add_lo; lo_ptr -= 1
    return float(poc), float(max(included) + BUCKET_SIZE), float(min(included))


# ── MACD engine (same as staggered_exit_sweep) ────────────────────────────────
def _ema(values, period):
    n = len(values); result = [None] * n
    if n < period: return result
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    k = 2.0 / (period + 1); prev = sma
    for i in range(period, n):
        v = values[i] * k + prev * (1.0 - k)
        result[i] = v; prev = v
    return result

def _macd(closes):
    n = len(closes)
    ef = _ema(closes, 12); es = _ema(closes, 26)
    ml = [(ef[i] - es[i]) if ef[i] is not None and es[i] is not None else None for i in range(n)]
    fv = next((i for i, v in enumerate(ml) if v is not None), -1)
    sl = [None] * n
    if fv == -1: return ml, sl
    vm = [ml[i] for i in range(fv, n)]
    es2 = _ema(vm, 9)
    for j, v in enumerate(es2):
        sl[fv + j] = v
    return ml, sl


# ── Option data loading (same as staggered_exit_sweep) ────────────────────────
@lru_cache(maxsize=512)
def _load_1m_opt(path_str: str) -> pd.DataFrame:
    path = DATA_ROOT / path_str
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for c in ("open","high","low","close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

@lru_cache(maxsize=512)
def _resample5(path_str: str) -> pd.DataFrame:
    df = _load_1m_opt(path_str)
    agg = {k:v for k,v in
           {"open":"first","high":"max","low":"min","close":"last","volume":"sum","oi":"last"}.items()
           if k in df.columns}
    r = (df.set_index("time")
           .resample("5min", label="right", closed="right")
           .agg(agg)
           .dropna(subset=["open","close"])
           .reset_index())
    return r

@dataclass(frozen=True)
class Desc:
    series_id: str
    underlying: str
    expiry_kind: str
    expiry: str
    strike: float
    ce_path: str
    pe_path: str
    pair_start: str

def _build_descs(kinds=("weekly",)) -> list[Desc]:
    raw = json.loads((DATA_ROOT / "contract_index.json").read_text())
    metas = [m for m in raw.values()
             if m.get("file_path") and m.get("candle_count") and
             m.get("earliest_candle") and m.get("strike") is not None and
             m.get("option_type") and m.get("expiry_kind") in kinds
             and m.get("underlying") == UNDERLYING]

    by_group = defaultdict(list)
    for m in metas:
        by_group[(m["underlying"], m["expiry_kind"], m["expiry"])].append(m)

    descs = []
    spot_df = _load_spot().set_index("time").sort_index()

    for (und, ek, exp), grp in sorted(by_group.items()):
        ce_map = {float(m["strike"]): m for m in grp if m["option_type"] == "CE"}
        pe_map = {float(m["strike"]): m for m in grp if m["option_type"] == "PE"}
        common = sorted(set(ce_map) & set(pe_map))
        if not common: continue
        candidates = []
        for st in common:
            ce = ce_map[st]; pe = pe_map[st]
            ps = max(pd.Timestamp(ce["earliest_candle"]), pd.Timestamp(pe["earliest_candle"]))
            pe2 = min(pd.Timestamp(ce["latest_candle"]),  pd.Timestamp(pe["latest_candle"]))
            if pe2 > ps: candidates.append((st, ps, ce, pe))
        if not candidates: continue
        start_day = min(p for _, p, _, _ in candidates).date()
        first_ts = min(p for _, p, _, _ in candidates)
        before = spot_df.loc[:first_ts]
        if before.empty: continue
        spot = float(before.iloc[-1]["close"])
        eligible = [c for c in candidates if c[1].date() == start_day] or candidates
        strike, pair_start, ce_m, pe_m = min(eligible, key=lambda c: (abs(c[0] - spot), c[1], c[0]))
        descs.append(Desc(
            series_id=f"{und}|{ek}|{exp}",
            underlying=und, expiry_kind=ek, expiry=exp,
            strike=float(strike),
            ce_path=ce_m["file_path"], pe_path=pe_m["file_path"],
            pair_start=pair_start.isoformat(),
        ))
    return descs


# ── Fixed-target simulator (same as baseline) ─────────────────────────────────
TARGET_PCT = 30.0
FLOOR_PCT  = -50.0

def _sim_target30(candles, entry_idx):
    ep = float(candles[entry_idx]["close"])
    tp = ep * 1.30
    for i in range(entry_idx + 1, len(candles)):
        if float(candles[i]["high"]) >= tp:
            return {"exit_idx": i, "blended_return": 30.0,
                    "exit_reason": "target_hit", "exit_time": str(candles[i]["time"])}
    i = len(candles) - 1
    cl = float(candles[i]["close"])
    return {"exit_idx": i, "blended_return": round((cl - ep) / ep * 100, 4),
            "exit_reason": "hold_to_end", "exit_time": str(candles[i]["time"])}

def _sim_mp_target(candles, entry_idx, target_pct):
    """Exit at a dynamically-computed % target (MP-derived)."""
    if target_pct <= 0:
        return _sim_target30(candles, entry_idx)
    ep = float(candles[entry_idx]["close"])
    tp = ep * (1.0 + target_pct / 100.0)
    for i in range(entry_idx + 1, len(candles)):
        if float(candles[i]["high"]) >= tp:
            return {"exit_idx": i, "blended_return": target_pct,
                    "exit_reason": "mp_target_hit", "exit_time": str(candles[i]["time"])}
    i = len(candles) - 1
    cl = float(candles[i]["close"])
    return {"exit_idx": i, "blended_return": round((cl - ep) / ep * 100, 4),
            "exit_reason": "hold_to_end", "exit_time": str(candles[i]["time"])}


# ── MP context at signal time ─────────────────────────────────────────────────
def _get_spot_at(spot_idx_df: pd.DataFrame, ts) -> Optional[float]:
    b = spot_idx_df.loc[:ts]
    if not b.empty: return float(b.iloc[-1]["close"])
    a = spot_idx_df.loc[ts:]
    if not a.empty: return float(a.iloc[0]["close"])
    return None


def _mp_context_at(signal_ts, signal_date, daily_mp_map: dict, prev_mp: Optional[DailyMP]):
    """
    Return dict of MP flags/levels at signal time.
    signal_ts: pandas Timestamp
    signal_date: date string (YYYY-MM-DD)
    daily_mp_map: {date_str -> DailyMP} for all days in series
    prev_mp: previous session's DailyMP (or None if first session)
    """
    today_mp = daily_mp_map.get(signal_date)
    ctx = {
        "has_mp": today_mp is not None,
        "poc": None, "vah": None, "val": None, "var": None,
        "ibh": None, "ibl": None, "ibr": None,
        "spot_above_poc": None, "spot_in_va": None,
        "spot_above_ibh": None, "spot_below_ibl": None,
        "fa_up": None, "fa_dn": None,
        "ppoc": None, "pvah": None, "pval": None,
        "spot_above_ppoc": None,
    }
    if today_mp is None:
        return ctx
    ctx.update({
        "poc": today_mp.poc, "vah": today_mp.vah, "val": today_mp.val,
        "var": today_mp.var, "ibh": today_mp.ibh, "ibl": today_mp.ibl,
        "ibr": today_mp.ibr, "fa_up": today_mp.fa_up, "fa_dn": today_mp.fa_dn,
    })
    if prev_mp:
        ctx.update({
            "ppoc": prev_mp.poc, "pvah": prev_mp.vah, "pval": prev_mp.val,
        })
    return ctx


# ── Strategy filters applied to MACD entries ─────────────────────────────────
def _apply_strategy(strategy: str, opt_type: str, spot: float,
                    ctx: dict, entry_pct: float) -> tuple[bool, float]:
    """
    Returns (take_trade: bool, target_override_pct: float).
    target_override_pct = 0 means use default target_30pct.
    """
    if not ctx["has_mp"]:
        # No MP data: fall through to default
        return True, 0.0

    poc  = ctx["poc"]
    vah  = ctx["vah"]
    val  = ctx["val"]
    ibh  = ctx["ibh"]
    ibl  = ctx["ibl"]
    ibr  = ctx["ibr"]
    ppoc = ctx["ppoc"]
    pvah = ctx["pvah"]
    pval = ctx["pval"]

    spot_above_poc  = spot > poc  if poc  else True
    spot_in_va      = (val <= spot <= vah) if (val and vah) else True
    spot_above_ibh  = spot > ibh  if ibh  else False
    spot_below_ibl  = spot < ibl  if ibl  else False
    spot_above_ppoc = spot > ppoc if ppoc else True

    if strategy == "baseline_both":
        return True, 0.0

    elif strategy == "mp_poc_filter":
        # CE only if spot above POC; PE only if spot below POC
        if opt_type == "CE": return spot_above_poc, 0.0
        else:                return not spot_above_poc, 0.0

    elif strategy == "mp_ppoc_filter":
        if ppoc is None: return True, 0.0
        if opt_type == "CE": return spot_above_ppoc, 0.0
        else:                return not spot_above_ppoc, 0.0

    elif strategy == "mp_ib_breakout":
        # CE only after IB broken upward; PE only after IB broken downward
        if opt_type == "CE": return spot_above_ibh, 0.0
        else:                return spot_below_ibl, 0.0

    elif strategy == "mp_va_outside":
        # Only enter when spot is OUTSIDE value area (breakout momentum)
        outside = not spot_in_va
        return outside, 0.0

    elif strategy == "mp_va_inside":
        # Only enter when spot is INSIDE value area (rotation/mean-reversion)
        return spot_in_va, 0.0

    elif strategy == "mp_ppoc_target":
        # All entries; use |PPOC − spot| / spot as option target proxy
        if ppoc is None: return True, 0.0
        spot_to_ppoc = abs(ppoc - spot) / spot * 100.0  # spot % move to PPOC
        # Option premium typically moves 3–5× spot move for ATM near-expiry
        opt_leverage = 3.5
        tgt = round(spot_to_ppoc * opt_leverage, 1)
        tgt = max(tgt, 15.0)   # minimum 15%
        return True, tgt

    elif strategy == "mp_fa_reversal":
        # Trade after a Failed Auction on previous session
        if ctx["fa_up"] or ctx["fa_dn"]:
            # FA_up = market tried to break up but failed → bearish → PE
            if ctx["fa_up"] and opt_type == "PE": return True, 0.0
            if ctx["fa_dn"] and opt_type == "CE": return True, 0.0
        return False, 0.0

    elif strategy == "mp_combo_best":
        # Must satisfy: POC filter + outside VA + IB break in direction
        poc_ok = (spot_above_poc if opt_type == "CE" else not spot_above_poc)
        va_ok  = not spot_in_va
        ib_ok  = (spot_above_ibh if opt_type == "CE" else spot_below_ibl)
        return (poc_ok and va_ok and ib_ok), 0.0

    elif strategy == "mp_poc_target":
        # Use today's POC as the target
        if poc is None: return True, 0.0
        spot_to_poc = abs(poc - spot) / spot * 100.0
        opt_leverage = 3.5
        tgt = max(round(spot_to_poc * opt_leverage, 1), 15.0)
        return True, tgt

    return True, 0.0


# ── Compounding helper ─────────────────────────────────────────────────────────
def _compound(returns, floor=FLOOR_PCT, alloc=0.20, start=100_000):
    eq = float(start)
    for r in returns:
        r = max(r, floor)
        eq = eq + eq * alloc * r / 100.0
    return eq


# ── Main ───────────────────────────────────────────────────────────────────────
STRATEGIES = [
    "baseline_both",
    "mp_poc_filter",
    "mp_ppoc_filter",
    "mp_ib_breakout",
    "mp_va_outside",
    "mp_va_inside",
    "mp_ppoc_target",
    "mp_fa_reversal",
    "mp_combo_best",
    "mp_poc_target",
]


def run():
    print("=" * 70)
    print("  Market Profile Analysis — SENSEX Weekly Series")
    print("=" * 70)

    # ── 1. Compute daily MP for all SENSEX trading days ───────────────────
    print("\n[1] Computing daily Market Profile …")
    spot_df = _load_spot()
    spot_df["date"] = spot_df["time"].dt.date

    daily_mp_all: dict[str, DailyMP] = {}
    for date, grp in spot_df.groupby("date"):
        mp = _compute_daily_mp(grp)
        if mp:
            daily_mp_all[str(date)] = mp

    print(f"    Daily MP computed for {len(daily_mp_all)} sessions")

    # Print sample MP stats
    vals = [(mp.poc, mp.vah, mp.val, mp.ibr) for mp in daily_mp_all.values()]
    pocs, vahs, vals_l, ibrs = zip(*vals)
    print(f"    POC  range : {min(pocs):.0f} – {max(pocs):.0f}")
    print(f"    Avg  VAR   : {np.mean([v-l for v,l in zip(vahs, vals_l)]):.0f} pts")
    print(f"    Avg  IBR   : {np.mean(ibrs):.0f} pts  |  Median IBR: {np.median(ibrs):.0f} pts")
    fa_up_cnt = sum(1 for mp in daily_mp_all.values() if mp.fa_up)
    fa_dn_cnt = sum(1 for mp in daily_mp_all.values() if mp.fa_dn)
    print(f"    FA Up      : {fa_up_cnt} sessions  |  FA Down: {fa_dn_cnt} sessions")

    # ── 2. Build series descriptors ───────────────────────────────────────
    print("\n[2] Building SENSEX weekly series descriptors …")
    descs = _build_descs(kinds=("weekly",))
    print(f"    Found {len(descs)} series")

    # ── 3. Build weekly MP per series ─────────────────────────────────────
    print("\n[3] Building weekly MP per series …")
    spot_idx = spot_df.set_index("time").sort_index()

    series_mp: dict[str, WeeklyMP] = {}
    sorted_descs = sorted(descs, key=lambda d: d.expiry)
    prev_weekly_poc = prev_weekly_vah = prev_weekly_val = None

    for desc in sorted_descs:
        start_ts = pd.Timestamp(desc.pair_start)
        expiry_ts = pd.Timestamp(desc.expiry)
        # Collect days from pair_start to expiry
        start_date  = start_ts.date() if hasattr(start_ts, 'date') else pd.Timestamp(start_ts).date()
        expiry_date = expiry_ts.date() if hasattr(expiry_ts, 'date') else pd.Timestamp(expiry_ts).date()
        series_dates = sorted([
            d for d in daily_mp_all
            if pd.Timestamp(d).date() >= start_date and pd.Timestamp(d).date() <= expiry_date
        ])
        day_mps = [daily_mp_all[d] for d in series_dates]

        if not day_mps:
            continue

        wpoc, wvah, wval = _compute_weekly_mp(day_mps)

        # Day-1 IB is the reference IB for the series
        d1 = day_mps[0]

        series_mp[desc.series_id] = WeeklyMP(
            series_id=desc.series_id,
            expiry=desc.expiry,
            daily_mps=day_mps,
            weekly_poc=wpoc, weekly_vah=wvah, weekly_val=wval,
            weekly_ibh=d1.ibh, weekly_ibl=d1.ibl, weekly_ibr=d1.ibr,
            ppoc=prev_weekly_poc or 0.0,
            pvah=prev_weekly_vah or 0.0,
            pval=prev_weekly_val or 0.0,
        )
        prev_weekly_poc = wpoc
        prev_weekly_vah = wvah
        prev_weekly_val = wval

    print(f"    Weekly MP built for {len(series_mp)} series")

    # ── 4. Run MACD strategies ─────────────────────────────────────────────
    print("\n[4] Running MP strategy sweep …")

    # Accumulate trades per strategy
    strat_trades: dict[str, list[float]] = {s: [] for s in STRATEGIES}
    strat_monthly: dict[str, dict[str, list[float]]] = {s: defaultdict(list) for s in STRATEGIES}

    # Shared advance function (target 30%)
    advance_fn = lambda c, i: _sim_target30(c, i)

    all_trade_rows = []

    for di, desc in enumerate(sorted_descs):
        if di % 5 == 0:
            print(f"    [{di+1}/{len(sorted_descs)}] {desc.series_id}")

        wmp = series_mp.get(desc.series_id)
        if wmp is None:
            continue

        # Build daily MP map for this series
        daily_mp_map = {d.date: d for d in wmp.daily_mps}
        # Build ordered day list for prev_mp lookup
        day_order = sorted(daily_mp_map.keys())

        for opt_type, path in (("CE", desc.ce_path), ("PE", desc.pe_path)):
            try:
                frame = _resample5(path)
            except FileNotFoundError:
                continue

            frame = frame[frame["time"] >= pd.Timestamp(desc.pair_start)].copy().reset_index(drop=True)
            if len(frame) < 40:
                continue

            candles = frame.to_dict("records")
            closes  = [float(c["close"]) for c in candles]
            ml, _   = _macd(closes)

            # Shared entry scan (same 263 entry universe)
            entries = []
            idx = 1
            while idx < len(candles):
                prev_ml = ml[idx - 1]; curr_ml = ml[idx]
                if prev_ml is None or curr_ml is None or not (prev_ml <= 0.0 and curr_ml > 0.0):
                    idx += 1; continue
                ep = float(candles[idx]["close"])
                if ep <= 0.0: idx += 1; continue
                entries.append(idx)
                res = advance_fn(candles, idx)
                idx = int(res["exit_idx"]) + 1

            if not entries:
                continue

            for entry_idx in entries:
                entry_ts   = pd.Timestamp(candles[entry_idx]["time"])
                entry_date = str(entry_ts.date())
                ep         = float(candles[entry_idx]["close"])

                # Get spot price at signal time
                sp = _get_spot_at(spot_idx, entry_ts)
                if sp is None: sp = 0.0

                # Get MP context
                date_idx_in_day_order = day_order.index(entry_date) if entry_date in day_order else -1
                prev_day_mp = daily_mp_map[day_order[date_idx_in_day_order - 1]] \
                    if date_idx_in_day_order > 0 else None
                ctx = _mp_context_at(entry_ts, entry_date, daily_mp_map, prev_day_mp)

                # Monthly key
                month_key = entry_ts.strftime("%Y-%m")

                # Run all strategies
                for strat in STRATEGIES:
                    take, tgt_override = _apply_strategy(strat, opt_type, sp, ctx, ep)
                    if not take:
                        continue
                    if tgt_override > 0:
                        res = _sim_mp_target(candles, entry_idx, tgt_override)
                    else:
                        res = _sim_target30(candles, entry_idx)

                    r = res["blended_return"]
                    strat_trades[strat].append(r)
                    strat_monthly[strat][month_key].append(r)

                    all_trade_rows.append({
                        "strategy": strat,
                        "series_id": desc.series_id,
                        "expiry": desc.expiry,
                        "option_type": opt_type,
                        "entry_time": str(entry_ts),
                        "entry_price": round(ep, 2),
                        "spot_at_entry": round(sp, 2),
                        "exit_time": res["exit_time"],
                        "exit_reason": res["exit_reason"],
                        "blended_return": r,
                        "poc": ctx.get("poc") or "",
                        "vah": ctx.get("vah") or "",
                        "val": ctx.get("val") or "",
                        "ibh": ctx.get("ibh") or "",
                        "ibl": ctx.get("ibl") or "",
                        "ppoc": ctx.get("ppoc") or "",
                        "target_override": round(tgt_override, 1),
                    })

    # ── 5. Print results table ─────────────────────────────────────────────
    print("\n" + "=" * 90)
    print(f"{'Strategy':<22} {'n':>5} {'WR%':>6} {'Avg%':>8} {'Med%':>8} "
          f"{'EV%':>7} {'FinalEq(₹L)':>13} {'PosMonths':>10}")
    print("=" * 90)

    results = {}
    for strat in STRATEGIES:
        rets = strat_trades[strat]
        n = len(rets)
        if n == 0:
            print(f"  {strat:<20} {'—':>5}")
            results[strat] = {"n": 0, "eq": 1.0}
            continue
        wr  = sum(1 for r in rets if r > 0) / n * 100
        avg = sum(rets) / n
        med = float(np.median(rets))
        wins = [r for r in rets if r > 0]; losses = [r for r in rets if r <= 0]
        aw = sum(wins) / len(wins) if wins else 0
        al = sum(losses) / len(losses) if losses else 0
        ev = wr / 100 * aw + (1 - wr / 100) * al

        eq    = _compound(sorted(rets, key=lambda r: str(r)))  # sort by entry_time in practice
        # Use properly time-sorted returns
        ts_sorted = sorted(all_trade_rows, key=lambda x: x["entry_time"])
        ts_rets   = [x["blended_return"] for x in ts_sorted if x["strategy"] == strat]
        eq        = _compound(ts_rets)

        months = strat_monthly[strat]
        pos_m  = sum(1 for v in months.values() if sum(v) > 0)
        tot_m  = len(months)

        print(f"  {strat:<22} {n:>5} {wr:>6.1f} {avg:>+8.2f} {med:>+8.2f} "
              f"{ev:>+7.2f} {eq/1e5:>12.3f}L  {pos_m:>4}/{tot_m}")
        results[strat] = {"n": n, "wr": wr, "avg": avg, "med": med, "ev": ev,
                          "eq": eq, "pos_months": pos_m, "tot_months": tot_m,
                          "rets": ts_rets}

    # ── 6. Monthly breakdown for top strategies ─────────────────────────────
    top3 = sorted([s for s in STRATEGIES if results[s]["n"] > 0],
                  key=lambda s: results[s]["eq"], reverse=True)[:3]

    print(f"\n=== Monthly breakdown — Top 3 strategies ===")
    all_months = sorted(set(
        k for s in top3 for k in strat_monthly[s].keys()
    ))

    hdr = f"{'Month':<9}" + "".join(f"  {s[:18]:>18}" for s in top3)
    print(hdr)
    print("-" * len(hdr))
    for m in all_months:
        row = f"  {m:<9}"
        for s in top3:
            v = strat_monthly[s].get(m, [])
            n = len(v); avg = sum(v) / n if n else 0
            row += f"  {n:>4}tr  {avg:>+6.1f}%"
        print(row)

    # ── 7. Daily MP statistics summary ────────────────────────────────────
    print(f"\n=== Daily Market Profile Statistics ===")
    mp_list = list(daily_mp_all.values())
    ibr_arr = [m.ibr for m in mp_list]
    var_arr = [m.var for m in mp_list]
    print(f"  IBR:  mean={np.mean(ibr_arr):.0f}  median={np.median(ibr_arr):.0f}  "
          f"p25={np.percentile(ibr_arr,25):.0f}  p75={np.percentile(ibr_arr,75):.0f}")
    print(f"  VAR:  mean={np.mean(var_arr):.0f}  median={np.median(var_arr):.0f}  "
          f"p25={np.percentile(var_arr,25):.0f}  p75={np.percentile(var_arr,75):.0f}")
    ib_broke_up = sum(1 for m in mp_list if m.ib_broken_up)
    ib_broke_dn = sum(1 for m in mp_list if m.ib_broken_dn)
    ib_both = sum(1 for m in mp_list if m.ib_broken_up and m.ib_broken_dn)
    ib_none = sum(1 for m in mp_list if not m.ib_broken_up and not m.ib_broken_dn)
    n = len(mp_list)
    print(f"  IB   broke UP  : {ib_broke_up}/{n}  ({ib_broke_up/n*100:.0f}%)")
    print(f"  IB   broke DN  : {ib_broke_dn}/{n}  ({ib_broke_dn/n*100:.0f}%)")
    print(f"  IB   broke BOTH: {ib_both}/{n}  ({ib_both/n*100:.0f}%)")
    print(f"  IB   held      : {ib_none}/{n}  ({ib_none/n*100:.0f}%)")
    print(f"  FA Up          : {fa_up_cnt}/{n}  ({fa_up_cnt/n*100:.0f}%)")
    print(f"  FA Down        : {fa_dn_cnt}/{n}  ({fa_dn_cnt/n*100:.0f}%)")

    # ── 8. Write trades CSV ────────────────────────────────────────────────
    csv_path = OUTPUT_ROOT / "mp_strategy_trades.csv"
    if all_trade_rows:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(all_trade_rows[0].keys()))
            w.writeheader(); w.writerows(all_trade_rows)
    print(f"\nWrote {len(all_trade_rows)} rows → {csv_path}")

    # ── 9. Write daily MP CSV ──────────────────────────────────────────────
    mp_csv = OUTPUT_ROOT / "daily_mp_params.csv"
    with open(mp_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "date","poc","vah","val","var","ibh","ibl","ibr",
            "ib_broken_up","ib_broken_dn","fa_up","fa_dn",
            "session_high","session_low","open_price","close_price","total_tpos"
        ])
        w.writeheader()
        for mp in sorted(daily_mp_all.values(), key=lambda m: m.date):
            w.writerow({k: getattr(mp, k) for k in w.fieldnames})
    print(f"Wrote daily MP params → {mp_csv}")

    # ── 10. Plots ──────────────────────────────────────────────────────────
    _plot_results(results, strat_monthly, all_months, daily_mp_all)

    print("\nDone.")
    return results, daily_mp_all


# ── Plots ──────────────────────────────────────────────────────────────────────
def _plot_results(results, strat_monthly, all_months, daily_mp_all):
    # ── Figure 1: Strategy comparison bar + equity curves ─────────────────
    active = [s for s in STRATEGIES if results[s]["n"] > 0]
    eqs    = [results[s]["eq"] / 1e5 for s in active]
    wrs    = [results[s]["wr"]        for s in active]
    ns     = [results[s]["n"]         for s in active]

    colours = ["#2ecc71" if eq >= (results["baseline_both"]["eq"] / 1e5) else "#e74c3c"
               for eq in eqs]

    fig, axes = plt.subplots(1, 2, figsize=(16, 7))
    fig.suptitle("Market Profile Strategy Sweep — SENSEX Weekly Options", fontsize=14, fontweight="bold")

    # Bar chart: final equity
    ax = axes[0]
    short_names = [s.replace("mp_","").replace("_"," ") for s in active]
    bars = ax.barh(short_names, eqs, color=colours, edgecolor="white", height=0.6)
    base_eq = results["baseline_both"]["eq"] / 1e5
    ax.axvline(base_eq, color="navy", linestyle="--", linewidth=1.5, label=f"Baseline ₹{base_eq:.2f}L")
    for bar, n, wr, eq in zip(bars, ns, wrs, eqs):
        ax.text(eq + 0.05, bar.get_y() + bar.get_height() / 2,
                f"  n={n}  WR={wr:.0f}%  ₹{eq:.2f}L",
                va="center", fontsize=8)
    ax.set_xlabel("Final Equity (₹ Lakhs)", fontsize=10)
    ax.set_title("Final Equity by Strategy (20% alloc, ₹1L start)", fontsize=11)
    ax.legend(fontsize=9)
    ax.set_xlim(0, max(eqs) * 1.35)

    # Equity curves for top 4
    ax2 = axes[1]
    top4 = sorted(active, key=lambda s: results[s]["eq"], reverse=True)[:4]
    ts_rows_by_strat = {}
    for strat in top4:
        ts_rows_by_strat[strat] = results[strat]["rets"]

    for strat in top4:
        rets = ts_rows_by_strat[strat]
        eq   = 100_000.0
        curve = [eq]
        for r in rets:
            r = max(r, FLOOR_PCT)
            eq = eq + eq * 0.20 * r / 100.0
            curve.append(eq)
        label = strat.replace("mp_","").replace("_"," ") + f" ₹{curve[-1]/1e5:.2f}L"
        ax2.plot(curve, linewidth=1.5, label=label)

    ax2.set_xlabel("Trade #", fontsize=10)
    ax2.set_ylabel("Equity (₹)", fontsize=10)
    ax2.set_title("Equity Curves — Top 4 Strategies", fontsize=11)
    ax2.legend(fontsize=8)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x/1e5:.1f}L"))
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    out1 = OUTPUT_ROOT / "mp_strategy_comparison.png"
    plt.savefig(out1, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Plot saved → {out1}")

    # ── Figure 2: Daily MP profile visualisation (sample week) ────────────
    # Pick a week in the middle of the dataset for illustration
    sample_days = sorted(daily_mp_all.values(), key=lambda m: m.date)
    mid = len(sample_days) // 2
    sample_week = sample_days[max(0, mid-2): mid+3]

    fig2, axes2 = plt.subplots(1, len(sample_week), figsize=(4 * len(sample_week), 9),
                                sharey=False)
    if len(sample_week) == 1: axes2 = [axes2]
    fig2.suptitle("Market Profile — Sample Week (TPO Distribution)", fontsize=13, fontweight="bold")

    for ax, mp in zip(axes2, sample_week):
        buckets = sorted(mp.tpo_counts.keys())
        counts  = [mp.tpo_counts[b] for b in buckets]
        colours_bar = []
        for b in buckets:
            if mp.val <= b < mp.vah:  colours_bar.append("#3498db")   # VA
            elif b == mp.poc:         colours_bar.append("#e74c3c")   # POC
            else:                     colours_bar.append("#bdc3c7")   # outside VA
        ax.barh([b + BUCKET_SIZE/2 for b in buckets], counts,
                height=BUCKET_SIZE * 0.9, color=colours_bar, edgecolor="none")
        ax.axhline(mp.poc + BUCKET_SIZE/2, color="#e74c3c", linewidth=2, linestyle="--", label=f"POC {mp.poc:.0f}")
        ax.axhline(mp.vah, color="#3498db", linewidth=1.5, linestyle="-", label=f"VAH {mp.vah:.0f}")
        ax.axhline(mp.val, color="#3498db", linewidth=1.5, linestyle="-", label=f"VAL {mp.val:.0f}")
        ax.axhline(mp.ibh, color="#f39c12", linewidth=1.5, linestyle=":", label=f"IBH {mp.ibh:.0f}")
        ax.axhline(mp.ibl, color="#f39c12", linewidth=1.5, linestyle=":", label=f"IBL {mp.ibl:.0f}")
        ax.set_title(f"{mp.date}\nPOC={mp.poc:.0f}  VAR={mp.var:.0f}\nIBR={mp.ibr:.0f}  "
                     f"{'FA↑' if mp.fa_up else ''}{'FA↓' if mp.fa_dn else ''}",
                     fontsize=8)
        ax.legend(fontsize=6, loc="upper right")
        ax.set_xlabel("TPOs", fontsize=8)
        if ax == axes2[0]: ax.set_ylabel("Price", fontsize=8)
        ax.tick_params(labelsize=7)

    plt.tight_layout()
    out2 = OUTPUT_ROOT / "mp_sample_profiles.png"
    plt.savefig(out2, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Plot saved → {out2}")

    # ── Figure 3: Monthly heatmap ─────────────────────────────────────────
    top6 = sorted(active, key=lambda s: results[s]["eq"], reverse=True)[:6]
    if all_months:
        month_avgs = {s: [np.mean(strat_monthly[s].get(m, [0])) for m in all_months]
                      for s in top6}
        heat_data = np.array([month_avgs[s] for s in top6])
        fig3, ax3 = plt.subplots(figsize=(max(12, len(all_months)), 5))
        im = ax3.imshow(heat_data, aspect="auto", cmap="RdYlGn",
                        vmin=-30, vmax=30)
        ax3.set_xticks(range(len(all_months))); ax3.set_xticklabels(all_months, rotation=45, ha="right", fontsize=8)
        ax3.set_yticks(range(len(top6)))
        ax3.set_yticklabels([s.replace("mp_","").replace("_"," ") for s in top6], fontsize=9)
        for i, s in enumerate(top6):
            for j, m in enumerate(all_months):
                v = month_avgs[s][j]
                ax3.text(j, i, f"{v:+.0f}", ha="center", va="center", fontsize=7,
                         color="black" if abs(v) < 20 else "white")
        plt.colorbar(im, ax=ax3, label="Avg Return % per month")
        ax3.set_title("Monthly Average Return % — Top 6 MP Strategies", fontsize=12, fontweight="bold")
        plt.tight_layout()
        out3 = OUTPUT_ROOT / "mp_monthly_heatmap.png"
        plt.savefig(out3, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"Plot saved → {out3}")

    # ── Figure 4: IB Range distribution + IBR vs daily range ─────────────
    mp_list = list(daily_mp_all.values())
    ibrs    = [m.ibr for m in mp_list]
    day_rng = [m.session_high - m.session_low for m in mp_list]
    ibr_ext = [(m.session_high - m.session_low) / m.ibr if m.ibr > 0 else 0 for m in mp_list]

    fig4, axes4 = plt.subplots(1, 3, figsize=(15, 5))
    fig4.suptitle("Market Profile — Initial Balance Statistics", fontsize=12, fontweight="bold")

    axes4[0].hist(ibrs, bins=30, color="#3498db", edgecolor="white", alpha=0.8)
    axes4[0].axvline(np.median(ibrs), color="red", linestyle="--", label=f"Median {np.median(ibrs):.0f}")
    axes4[0].axvline(np.mean(ibrs), color="orange", linestyle="-", label=f"Mean {np.mean(ibrs):.0f}")
    axes4[0].set_title("IB Range Distribution"); axes4[0].set_xlabel("IB Range (pts)")
    axes4[0].legend(fontsize=8)

    axes4[1].scatter(ibrs, day_rng, alpha=0.4, s=20, color="#2ecc71")
    m, b = np.polyfit(ibrs, day_rng, 1)
    xi = np.linspace(min(ibrs), max(ibrs), 100)
    axes4[1].plot(xi, m*xi+b, "r--", linewidth=1.5, label=f"y={m:.2f}x+{b:.0f}")
    axes4[1].set_xlabel("IB Range"); axes4[1].set_ylabel("Daily Range")
    axes4[1].set_title(f"IB Range vs Daily Range\nCorr={np.corrcoef(ibrs,day_rng)[0,1]:.3f}")
    axes4[1].legend(fontsize=8)

    axes4[2].hist(ibr_ext, bins=30, color="#e74c3c", edgecolor="white", alpha=0.8)
    axes4[2].axvline(1.0, color="black", linestyle="--", linewidth=1.5, label="1× (IB = Day Range)")
    axes4[2].axvline(np.median(ibr_ext), color="blue", linestyle="--",
                     label=f"Median {np.median(ibr_ext):.2f}×")
    axes4[2].set_title("Day Range / IB Range Ratio")
    axes4[2].set_xlabel("Extension Ratio"); axes4[2].legend(fontsize=8)

    plt.tight_layout()
    out4 = OUTPUT_ROOT / "mp_ib_statistics.png"
    plt.savefig(out4, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Plot saved → {out4}")


if __name__ == "__main__":
    run()
