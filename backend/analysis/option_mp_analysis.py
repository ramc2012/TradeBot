"""
Option Market Profile Analysis — SENSEX Weekly ATM CE & PE
==========================================================

Computes Market Profile parameters DIRECTLY on ATM option premium 1-min candles
(NOT on spot data) and tests MP-based trading strategies.

Key difference from spot MP:
  - Option IBR reflects the ACTUAL option price range in first 60 min
  - Option POC = most-visited premium price bucket (10-pt buckets)
  - Option IB extension = option price breaks its own IBH/IBL
    → CE above its IBH = underlying strongly bullish, option in breakout
    → PE above its IBH = underlying strongly bearish, option in breakout

Strategies tested:
  0. baseline_both          — all MACD signals, 20% alloc (reference)
  1. opt_ib_ext_ce          — CE: only enter when option already > IBH
  2. opt_ib_ext_pe          — PE: only enter when option already > its IBH
  3. opt_ib_ext_any         — either CE or PE IB extended at signal time
  4. opt_ib_ext_alloc       — all signals; 35% alloc when IB extended, 10% rest
  5. opt_poc_reversion      — CE below opt_POC, PE above opt_POC (reversion to POC)
  6. opt_poc_alloc          — 35% when POC reversion, 10% rest
  7. opt_va_break           — enter when option breaks outside VA in signal direction
  8. opt_ibr_target         — all entries; exit at option's own IBH+1×IBR (CE) or IBL-1×IBR (PE)
  9. opt_ib_ext_poc_combo   — IB extension AND POC reversion (strongest combo)
  10. opt_ib_ext_alloc_ibr_tgt — IB extension sizing + IBR-based dynamic target
  11. spot_poc_reversion     — REFERENCE: spot-based POC reversion (from prev analysis)
  12. spot_poc_alloc         — REFERENCE: spot-based variable alloc

Output: runtime/index_analytics_data/option_mp/
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

# ── Paths ──────────────────────────────────────────────────────────────────────
DATA_ROOT   = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"
OUTPUT_ROOT = DATA_ROOT / "option_mp"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

UNDERLYING       = "SENSEX"
OPT_BUCKET       = 10      # 10-pt premium buckets for option TPOs
TPO_MINUTES      = 30      # 30-min TPO periods
IB_MINUTES       = 60      # Initial Balance = first 60 min (9:15–10:15)
VA_PCT           = 0.70
MIN_IB_CANDLES   = 30      # minimum 1-min candles in IB period to trust IBH/IBL
TARGET_PCT       = 30.0    # default exit target
FLOOR_PCT        = -50.0   # compounding floor
ALLOC_HIGH       = 0.35    # high-conviction allocation
ALLOC_LOW        = 0.10    # low-conviction allocation
ALLOC_BASE       = 0.20    # baseline allocation

# ── Data loading ───────────────────────────────────────────────────────────────
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
           {"open":"first","high":"max","low":"min","close":"last","volume":"sum","oi":"last"}.items()
           if k in df.columns}
    r = (df.set_index("time")
           .resample("5min", label="right", closed="right")
           .agg(agg).dropna(subset=["open","close"]).reset_index())
    return r

@lru_cache(maxsize=4)
def _spot_df() -> pd.DataFrame:
    return _load_1m(f"spot/underlying={UNDERLYING}/1minute.csv.gz")


# ── Option MP engine ───────────────────────────────────────────────────────────
@dataclass
class OptionDayMP:
    date:          str
    option_type:   str
    poc:           float
    vah:           float
    val:           float
    ibh:           float   # option price high in first 60 min
    ibl:           float   # option price low in first 60 min
    ibr:           float   # ibh - ibl
    ib_open:       float   # option open price
    ib_ext_up:     bool    # option closed above its own IBH during session
    ib_ext_dn:     bool    # option closed below its own IBL
    ext_up_pct:    float   # how far above IBH session high went (%)
    ext_dn_pct:    float   # how far below IBL session low went (%)
    session_high:  float
    session_low:   float
    day_range:     float
    fa_up:         bool
    fa_dn:         bool
    # Derived targets
    ce_ibr_target: float   # IBH + 1× IBR → CE target
    pe_ibr_target: float   # IBL − 1× IBR → PE target (only if CE option)
    ibr_target_pct: float  # ((ce_ibr_target - ib_open) / ib_open) × 100


def _compute_option_day_mp(day_df: pd.DataFrame, opt_type: str) -> Optional[OptionDayMP]:
    """Compute MP from 1-min option candles for one trading day."""
    day_df = day_df.copy().reset_index(drop=True)
    times  = day_df["time"]
    if len(day_df) < MIN_IB_CANDLES:
        return None

    session_start = times.iloc[0]
    date_str      = str(session_start.date())

    # ── IB period ───────────────────────────────────────────────────────────
    ib_mask  = times < session_start + pd.Timedelta(minutes=IB_MINUTES)
    ib_df    = day_df[ib_mask]
    if len(ib_df) < MIN_IB_CANDLES:
        return None

    ibh      = float(ib_df["high"].max())
    ibl      = float(ib_df["low"].min())
    ibr      = ibh - ibl
    ib_open  = float(ib_df["open"].iloc[0])
    if ibr <= 0 or ib_open <= 0:
        return None

    # ── TPO counts (30-min periods) ──────────────────────────────────────────
    tpo_counts: dict[float, int] = defaultdict(int)
    tpo_periods = (day_df.set_index("time")
                   .resample(f"{TPO_MINUTES}min", label="left", closed="left")
                   .agg({"high": "max", "low": "min"}).dropna())

    for _, row in tpo_periods.iterrows():
        lo_b = math.floor(float(row["low"])  / OPT_BUCKET) * OPT_BUCKET
        hi_b = math.floor(float(row["high"]) / OPT_BUCKET) * OPT_BUCKET
        b = lo_b
        while b <= hi_b:
            tpo_counts[b] += 1
            b += OPT_BUCKET

    if not tpo_counts:
        return None

    total_tpos = sum(tpo_counts.values())

    # ── POC ──────────────────────────────────────────────────────────────────
    poc = float(max(tpo_counts, key=lambda b: tpo_counts[b]))

    # ── Value Area ────────────────────────────────────────────────────────────
    sorted_b  = sorted(tpo_counts.keys())
    poc_idx   = sorted_b.index(poc)
    included  = {poc}
    inc_vol   = tpo_counts[poc]
    target_v  = math.ceil(total_tpos * VA_PCT)
    lo_ptr    = poc_idx - 1
    hi_ptr    = poc_idx + 1
    while inc_vol < target_v:
        add_lo = tpo_counts.get(sorted_b[lo_ptr], 0) if lo_ptr >= 0 else 0
        add_hi = tpo_counts.get(sorted_b[hi_ptr], 0) if hi_ptr < len(sorted_b) else 0
        if add_lo == 0 and add_hi == 0:
            break
        if add_hi >= add_lo:
            included.add(sorted_b[hi_ptr]); inc_vol += add_hi; hi_ptr += 1
        else:
            included.add(sorted_b[lo_ptr]); inc_vol += add_lo; lo_ptr -= 1

    vah = float(max(included)) + OPT_BUCKET
    val = float(min(included))

    # ── IB extension & FA ────────────────────────────────────────────────────
    post_ib      = day_df[~ib_mask]
    session_high = float(day_df["high"].max())
    session_low  = float(day_df["low"].min())
    ib_ext_up    = session_high > ibh
    ib_ext_dn    = session_low  < ibl
    ext_up_pct   = (session_high - ibh) / ibh * 100 if ib_ext_up else 0.0
    ext_dn_pct   = (ibl - session_low)  / ibl * 100 if ib_ext_dn else 0.0

    last_close   = float(day_df["close"].iloc[-1])
    fa_up = ib_ext_up and last_close < ibh
    fa_dn = ib_ext_dn and last_close > ibl

    # ── IBR-based targets ────────────────────────────────────────────────────
    ce_ibr_target   = ibh + ibr              # 1× IBR extension above IBH
    pe_ibr_target   = max(ibl - ibr, 1.0)   # 1× IBR below IBL (never < 1)
    ibr_target_pct  = (ce_ibr_target - ib_open) / ib_open * 100.0

    return OptionDayMP(
        date=date_str, option_type=opt_type,
        poc=poc, vah=vah, val=val,
        ibh=ibh, ibl=ibl, ibr=ibr, ib_open=ib_open,
        ib_ext_up=ib_ext_up, ib_ext_dn=ib_ext_dn,
        ext_up_pct=ext_up_pct, ext_dn_pct=ext_dn_pct,
        session_high=session_high, session_low=session_low,
        day_range=session_high - session_low,
        fa_up=fa_up, fa_dn=fa_dn,
        ce_ibr_target=ce_ibr_target, pe_ibr_target=pe_ibr_target,
        ibr_target_pct=ibr_target_pct,
    )


# ── Series descriptors ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Desc:
    series_id:  str
    underlying: str
    expiry_kind: str
    expiry:     str
    strike:     float
    ce_path:    str
    pe_path:    str
    pair_start: str

def _build_descs() -> list[Desc]:
    raw    = json.loads((DATA_ROOT / "contract_index.json").read_text())
    metas  = [m for m in raw.values()
              if m.get("file_path") and m.get("candle_count") and
              m.get("earliest_candle") and m.get("strike") is not None and
              m.get("option_type") and m.get("expiry_kind") == "weekly"
              and m.get("underlying") == UNDERLYING]

    by_group: dict = defaultdict(list)
    for m in metas:
        by_group[(m["underlying"], m["expiry_kind"], m["expiry"])].append(m)

    descs  = []
    spot   = _spot_df().set_index("time").sort_index()

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
        start_day  = min(p for _, p, _, _ in candidates).date()
        first_ts   = min(p for _, p, _, _ in candidates)
        before     = spot.loc[:first_ts]
        if before.empty: continue
        sp         = float(before.iloc[-1]["close"])
        eligible   = [c for c in candidates if c[1].date() == start_day] or candidates
        strike, pair_start, ce_m, pe_m = min(eligible, key=lambda c: (abs(c[0] - sp), c[1], c[0]))
        descs.append(Desc(
            series_id=f"{und}|{ek}|{exp}",
            underlying=und, expiry_kind=ek, expiry=exp,
            strike=float(strike),
            ce_path=ce_m["file_path"], pe_path=pe_m["file_path"],
            pair_start=pair_start.isoformat(),
        ))
    return descs


# ── MACD engine ────────────────────────────────────────────────────────────────
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
    n  = len(closes)
    ef = _ema(closes, 12); es = _ema(closes, 26)
    ml = [(ef[i] - es[i]) if ef[i] is not None and es[i] is not None else None for i in range(n)]
    fv = next((i for i, v in enumerate(ml) if v is not None), -1)
    sl = [None] * n
    if fv == -1: return ml, sl
    es2 = _ema([ml[i] for i in range(fv, n)], 9)
    for j, v in enumerate(es2): sl[fv + j] = v
    return ml, sl


# ── Trade simulators ───────────────────────────────────────────────────────────
def _sim_target(candles, entry_idx: int, target_pct: float) -> dict:
    ep = float(candles[entry_idx]["close"])
    tp = ep * (1.0 + target_pct / 100.0)
    for i in range(entry_idx + 1, len(candles)):
        if float(candles[i]["high"]) >= tp:
            return {"exit_idx": i, "blended_return": target_pct,
                    "exit_reason": "target_hit", "exit_time": str(candles[i]["time"])}
    i  = len(candles) - 1
    cl = float(candles[i]["close"])
    return {"exit_idx": i,
            "blended_return": round((cl - ep) / ep * 100.0, 4),
            "exit_reason": "hold_to_end", "exit_time": str(candles[i]["time"])}


# ── Compounding ────────────────────────────────────────────────────────────────
def _compound(rets, alloc=ALLOC_BASE, floor=FLOOR_PCT, start=100_000.0):
    eq = float(start)
    for r in rets:
        eq = eq + eq * alloc * max(r, floor) / 100.0
    return eq

def _compound_variable(rows, floor=FLOOR_PCT, start=100_000.0):
    """rows = list of {blended_return, alloc}"""
    eq = float(start)
    for row in rows:
        eq = eq + eq * row["alloc"] * max(row["blended_return"], floor) / 100.0
    return eq


# ── Option MP context at signal candle ────────────────────────────────────────
def _option_mp_at_signal(signal_ts, signal_date: str,
                         opt_mp_map: dict) -> Optional[OptionDayMP]:
    """Return the OptionDayMP for this session (if computed before signal_ts)."""
    mp = opt_mp_map.get(signal_date)
    if mp is None:
        return None
    # IB window ends 60 min after 09:15 → 10:15
    # Signal must be AFTER the IB window for IBH/IBL to be known
    ib_end_ts = pd.Timestamp(f"{signal_date} 10:15:00").tz_localize(signal_ts.tzinfo)
    if signal_ts <= ib_end_ts:
        return None   # signal is still inside IB — IBH/IBL not yet confirmed
    return mp


# ── Spot MP reference (from previous analysis) ────────────────────────────────
def _load_spot_mp() -> dict[str, dict]:
    """Load daily_mp_params.csv from spot-based market_profile analysis."""
    path = DATA_ROOT / "market_profile" / "daily_mp_params.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    return {str(row["date"]): row.to_dict() for _, row in df.iterrows()}


# ── Main ───────────────────────────────────────────────────────────────────────
def run():
    print("=" * 72)
    print("  Option Market Profile Analysis — SENSEX Weekly CE & PE")
    print("=" * 72)

    # ── 1. Build descriptors ──────────────────────────────────────────────
    print("\n[1] Building series descriptors …")
    descs = _build_descs()
    print(f"    {len(descs)} SENSEX weekly series found")

    # ── 2. Compute option MP per series per session ───────────────────────
    print("\n[2] Computing option Market Profile per series-day …")
    # ce_mp_all[series_id][date] = OptionDayMP
    ce_mp_all: dict[str, dict[str, OptionDayMP]] = {}
    pe_mp_all: dict[str, dict[str, OptionDayMP]] = {}

    for desc in sorted(descs, key=lambda d: d.expiry):
        for opt_type, path, mp_store in [
            ("CE", desc.ce_path, ce_mp_all),
            ("PE", desc.pe_path, pe_mp_all),
        ]:
            try:
                raw1m = _load_1m(path)
            except FileNotFoundError:
                continue

            raw1m = raw1m[raw1m["time"] >= pd.Timestamp(desc.pair_start)].copy().reset_index(drop=True)
            raw1m["date"] = raw1m["time"].dt.date

            day_mps: dict[str, OptionDayMP] = {}
            for date, day_df in raw1m.groupby("date"):
                mp = _compute_option_day_mp(day_df, opt_type)
                if mp:
                    day_mps[str(date)] = mp

            mp_store[desc.series_id] = day_mps

    total_ce_days = sum(len(v) for v in ce_mp_all.values())
    total_pe_days = sum(len(v) for v in pe_mp_all.values())
    print(f"    CE MP: {total_ce_days} session-days across all series")
    print(f"    PE MP: {total_pe_days} session-days across all series")

    # ── 3. Aggregate option MP statistics ─────────────────────────────────
    print("\n[3] Option MP statistics …")
    all_ce_mps = [mp for d in ce_mp_all.values() for mp in d.values()]
    all_pe_mps = [mp for d in pe_mp_all.values() for mp in d.values()]

    for name, mps in [("CE", all_ce_mps), ("PE", all_pe_mps)]:
        ibrs    = [m.ibr for m in mps]
        ext_up  = [m for m in mps if m.ib_ext_up]
        ext_dn  = [m for m in mps if m.ib_ext_dn]
        fa_up   = [m for m in mps if m.fa_up]
        fa_dn   = [m for m in mps if m.fa_dn]
        ratios  = [m.day_range / m.ibr for m in mps if m.ibr > 0]
        print(f"\n  {name} option ({len(mps)} sessions):")
        print(f"    IBR: mean={np.mean(ibrs):.1f}  median={np.median(ibrs):.1f}  "
              f"p25={np.percentile(ibrs,25):.1f}  p75={np.percentile(ibrs,75):.1f}")
        print(f"    IB ext UP  : {len(ext_up)}/{len(mps)} ({len(ext_up)/len(mps)*100:.0f}%)  "
              f"avg ext={np.mean([m.ext_up_pct for m in ext_up]):.1f}%" if ext_up else
              f"    IB ext UP  : 0/{len(mps)}")
        print(f"    IB ext DN  : {len(ext_dn)}/{len(mps)} ({len(ext_dn)/len(mps)*100:.0f}%)  "
              f"avg ext={np.mean([m.ext_dn_pct for m in ext_dn]):.1f}%" if ext_dn else
              f"    IB ext DN  : 0/{len(mps)}")
        print(f"    FA Up/Dn   : {len(fa_up)} / {len(fa_dn)}")
        print(f"    Day/IBR    : median={np.median(ratios):.2f}×  p75={np.percentile(ratios,75):.2f}×")
        tgt_pcts = [m.ibr_target_pct for m in mps if m.ibr_target_pct > 0]
        print(f"    IBR target%: mean={np.mean(tgt_pcts):.1f}%  median={np.median(tgt_pcts):.1f}%")

    # ── 4. Load spot MP reference ──────────────────────────────────────────
    spot_mp = _load_spot_mp()

    # ── 5. Run strategies on all MACD signals ─────────────────────────────
    print("\n[4] Running strategy sweep …")

    # Storage
    STRATEGIES = [
        "baseline_both",
        "opt_ib_ext_ce_only",
        "opt_ib_ext_pe_only",
        "opt_ib_ext_alloc",
        "opt_poc_reversion",
        "opt_poc_alloc",
        "opt_va_break",
        "opt_ibr_target",
        "opt_ib_ext_ibr_tgt",
        "opt_ib_ext_poc_combo",
        "spot_poc_reversion",
        "spot_poc_alloc",
    ]

    strat_rows:    dict[str, list[dict]] = {s: [] for s in STRATEGIES}
    all_trade_csv: list[dict] = []

    advance_fn = lambda c, i: _sim_target(c, i, TARGET_PCT)

    for di, desc in enumerate(sorted(descs, key=lambda d: d.expiry)):
        if di % 5 == 0:
            print(f"    [{di+1}/{len(descs)}] {desc.series_id}")

        ce_day_mps = ce_mp_all.get(desc.series_id, {})
        pe_day_mps = pe_mp_all.get(desc.series_id, {})

        for opt_type, path in [("CE", desc.ce_path), ("PE", desc.pe_path)]:
            try:
                frame5 = _resample5(path)
            except FileNotFoundError:
                continue

            frame5 = frame5[frame5["time"] >= pd.Timestamp(desc.pair_start)].copy().reset_index(drop=True)
            if len(frame5) < 40:
                continue

            candles = frame5.to_dict("records")
            closes  = [float(c["close"]) for c in candles]
            ml, _   = _macd(closes)

            # Shared entry scan
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

            # Option day MP maps (both CE and PE may be needed)
            opt_day_mps = ce_day_mps if opt_type == "CE" else pe_day_mps

            for entry_idx in entries:
                entry_ts   = pd.Timestamp(candles[entry_idx]["time"])
                entry_date = str(entry_ts.date())
                ep         = float(candles[entry_idx]["close"])
                month_key  = entry_ts.strftime("%Y-%m")

                # Get option MP at signal time (only valid AFTER IB period)
                opt_mp = _option_mp_at_signal(entry_ts, entry_date, opt_day_mps)

                # Get spot MP for reference
                smp = spot_mp.get(entry_date, {})
                spot_poc = float(smp.get("poc", 0) or 0)

                # Get spot price from spot data
                spot_series = _spot_df().set_index("time").sort_index()
                b = spot_series.loc[:entry_ts]
                spot_px = float(b.iloc[-1]["close"]) if not b.empty else 0.0

                # ── Compute flags ────────────────────────────────────────────
                has_opt_mp     = opt_mp is not None

                # Option IB extension: for CE, we want CE price above IBH (bullish run)
                # For PE, we want PE price above IBH (bearish underlying run)
                opt_ib_ext     = has_opt_mp and opt_mp.ib_ext_up
                opt_ib_ext_dn  = has_opt_mp and opt_mp.ib_ext_dn

                # Option POC reversion: at signal time, is current price below POC?
                # (means it can revert upward → good for buying options)
                opt_poc_rev    = has_opt_mp and (ep < opt_mp.poc)

                # Option VA break: price above VAH (breakout momentum)
                opt_va_break   = has_opt_mp and (ep > opt_mp.vah)

                # Spot POC: for CE buy spot should be below spot POC (reversion up)
                # For PE buy spot should be above spot POC (reversion down)
                if spot_poc > 0:
                    spot_poc_rev = (opt_type == "CE" and spot_px < spot_poc) or \
                                   (opt_type == "PE" and spot_px > spot_poc)
                else:
                    spot_poc_rev = False

                # IBR-based target (only if opt_mp available)
                if has_opt_mp and opt_mp.ibr > 0:
                    # Target = IBH + 1×IBR for CE, entry_price → ce_ibr_target
                    # Convert to % from current entry price
                    ibr_tgt_pct = (opt_mp.ce_ibr_target - ep) / ep * 100.0
                    ibr_tgt_pct = max(ibr_tgt_pct, 15.0)
                else:
                    ibr_tgt_pct = TARGET_PCT

                # ── Per-strategy decisions ────────────────────────────────
                strategy_decisions = {
                    "baseline_both":         (True,         TARGET_PCT,  ALLOC_BASE),
                    "opt_ib_ext_ce_only":    (opt_type=="CE" and opt_ib_ext,
                                              TARGET_PCT,   ALLOC_BASE),
                    "opt_ib_ext_pe_only":    (opt_type=="PE" and opt_ib_ext,
                                              TARGET_PCT,   ALLOC_BASE),
                    "opt_ib_ext_alloc":      (True,
                                              TARGET_PCT,
                                              ALLOC_HIGH if opt_ib_ext else ALLOC_LOW),
                    "opt_poc_reversion":     (opt_poc_rev,  TARGET_PCT,  ALLOC_BASE),
                    "opt_poc_alloc":         (True,
                                              TARGET_PCT,
                                              ALLOC_HIGH if opt_poc_rev else ALLOC_LOW),
                    "opt_va_break":          (opt_va_break, TARGET_PCT,  ALLOC_BASE),
                    "opt_ibr_target":        (True,         ibr_tgt_pct, ALLOC_BASE),
                    "opt_ib_ext_ibr_tgt":   (True,
                                              ibr_tgt_pct if opt_ib_ext else TARGET_PCT,
                                              ALLOC_HIGH if opt_ib_ext else ALLOC_LOW),
                    "opt_ib_ext_poc_combo":  (opt_ib_ext and opt_poc_rev,
                                              TARGET_PCT,   ALLOC_BASE),
                    "spot_poc_reversion":    (spot_poc_rev, TARGET_PCT,  ALLOC_BASE),
                    "spot_poc_alloc":        (True,
                                              TARGET_PCT,
                                              ALLOC_HIGH if spot_poc_rev else ALLOC_LOW),
                }

                for strat, (take, tgt, alloc) in strategy_decisions.items():
                    if not take:
                        continue

                    res = _sim_target(candles, entry_idx, tgt)
                    r   = res["blended_return"]

                    strat_rows[strat].append({
                        "blended_return": r,
                        "alloc": alloc,
                        "entry_time": str(entry_ts),
                        "month": month_key,
                    })

                    if strat in ("baseline_both", "opt_ib_ext_alloc", "spot_poc_alloc",
                                 "opt_poc_alloc", "opt_ib_ext_ibr_tgt"):
                        all_trade_csv.append({
                            "strategy": strat,
                            "series_id": desc.series_id,
                            "expiry": desc.expiry,
                            "option_type": opt_type,
                            "entry_time": str(entry_ts),
                            "entry_price": round(ep, 2),
                            "spot_px": round(spot_px, 2),
                            "exit_time": res["exit_time"],
                            "exit_reason": res["exit_reason"],
                            "blended_return": r,
                            "alloc": alloc,
                            "target_used": round(tgt, 1),
                            "opt_ibh": round(opt_mp.ibh, 2) if opt_mp else "",
                            "opt_poc": round(opt_mp.poc, 2) if opt_mp else "",
                            "opt_ib_ext_up": opt_ib_ext if opt_mp else "",
                            "ibr_tgt_pct": round(ibr_tgt_pct, 1) if opt_mp else "",
                            "spot_poc_rev": spot_poc_rev,
                        })

    # ── 6. Print results table ─────────────────────────────────────────────
    print("\n" + "=" * 95)
    print(f"{'Strategy':<26} {'n':>5} {'WR%':>6} {'Avg%':>8} {'Med%':>8} "
          f"{'EV%':>7} {'FinalEq':>10} {'PosM':>6} {'vs BL':>7}")
    print("=" * 95)

    baseline_eq = None
    results = {}

    for strat in STRATEGIES:
        rows = sorted(strat_rows[strat], key=lambda r: r["entry_time"])
        n    = len(rows)
        if n == 0:
            print(f"  {strat:<24}  {'—':>5}")
            results[strat] = {"n": 0, "eq": 100_000.0, "rows": []}
            continue

        rets  = [r["blended_return"] for r in rows]
        allocs= [r["alloc"]          for r in rows]
        wr    = sum(1 for r in rets if r > 0) / n * 100
        avg   = sum(rets) / n
        med   = float(np.median(rets))
        wins  = [r for r in rets if r > 0]; losses = [r for r in rets if r <= 0]
        aw    = sum(wins)   / len(wins)   if wins   else 0
        al    = sum(losses) / len(losses) if losses else 0
        ev    = wr / 100 * aw + (1 - wr / 100) * al

        # Equity (variable alloc where applicable)
        eq    = _compound_variable([{"blended_return": r, "alloc": a}
                                    for r, a in zip(rets, allocs)])

        months   = defaultdict(list)
        for row in rows:
            months[row["month"]].append(row["blended_return"])
        pos_m = sum(1 for v in months.values() if sum(v) > 0)
        tot_m = len(months)

        if baseline_eq is None and strat == "baseline_both":
            baseline_eq = eq

        vs_bl = f"{(eq - baseline_eq) / baseline_eq * 100:+.1f}%" if baseline_eq else "—"

        print(f"  {strat:<26} {n:>5} {wr:>6.1f} {avg:>+8.2f} {med:>+8.2f} "
              f"{ev:>+7.2f} {eq/1e5:>9.3f}L  {pos_m:>2}/{tot_m} {vs_bl:>8}")

        results[strat] = {
            "n": n, "wr": wr, "avg": avg, "med": med, "ev": ev,
            "eq": eq, "pos_m": pos_m, "tot_m": tot_m,
            "rets": rets, "allocs": allocs, "rows": rows,
        }

    # ── 7. Detailed monthly for top strategies ─────────────────────────────
    top3 = sorted([s for s in STRATEGIES if results[s]["n"] > 0],
                  key=lambda s: results[s]["eq"], reverse=True)[:3]
    all_months = sorted(set(
        r["month"] for s in top3 for r in strat_rows[s]
    ))
    print(f"\n=== Monthly breakdown — Top 3 ===")
    print(f"{'Month':<9}" + "".join(f"  {s[:22]:>22}" for s in top3))
    print("-" * (9 + 26 * len(top3)))
    for m in all_months:
        row_str = f"  {m:<9}"
        for s in top3:
            v = [r["blended_return"] for r in strat_rows[s] if r["month"] == m]
            n = len(v); avg = sum(v) / n if n else 0
            allocs_m = [r["alloc"] for r in strat_rows[s] if r["month"] == m]
            row_str += f"  {n:>3}tr {avg:>+6.1f}% (alloc~{np.mean(allocs_m)*100:.0f}%)"
        print(row_str)

    # ── 8. IB extension analysis breakdown ────────────────────────────────
    print("\n=== IB Extension Impact on Win Rate ===")
    base_rows = strat_rows["baseline_both"]
    ib_alloc_rows = strat_rows["opt_ib_ext_alloc"]
    extended = [r for r in ib_alloc_rows if r["alloc"] == ALLOC_HIGH]
    not_ext  = [r for r in ib_alloc_rows if r["alloc"] == ALLOC_LOW]
    print(f"  With opt IB extension (alloc={ALLOC_HIGH*100:.0f}%): "
          f"n={len(extended)}  "
          f"WR={(sum(1 for r in extended if r['blended_return']>0)/len(extended)*100 if extended else 0):.1f}%  "
          f"avg={(sum(r['blended_return'] for r in extended)/len(extended) if extended else 0):+.2f}%")
    print(f"  Without IB extension  (alloc={ALLOC_LOW*100:.0f}%):  "
          f"n={len(not_ext)}  "
          f"WR={(sum(1 for r in not_ext if r['blended_return']>0)/len(not_ext)*100 if not_ext else 0):.1f}%  "
          f"avg={(sum(r['blended_return'] for r in not_ext)/len(not_ext) if not_ext else 0):+.2f}%")

    # ── 9. Write CSV ───────────────────────────────────────────────────────
    if all_trade_csv:
        csv_path = OUTPUT_ROOT / "option_mp_trades.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(all_trade_csv[0].keys()))
            w.writeheader(); w.writerows(all_trade_csv)
        print(f"\nWrote {len(all_trade_csv)} rows → {csv_path}")

    # ── 10. Write option MP params CSV ────────────────────────────────────
    mp_rows = []
    for series_id, day_mps in {**ce_mp_all, **pe_mp_all}.items():
        for date_str, mp in day_mps.items():
            mp_rows.append({
                "series_id": series_id, "date": date_str,
                "option_type": mp.option_type,
                "poc": mp.poc, "vah": mp.vah, "val": mp.val,
                "ibh": mp.ibh, "ibl": mp.ibl, "ibr": mp.ibr,
                "ib_open": mp.ib_open,
                "ib_ext_up": mp.ib_ext_up, "ib_ext_dn": mp.ib_ext_dn,
                "ext_up_pct": round(mp.ext_up_pct, 2),
                "ext_dn_pct": round(mp.ext_dn_pct, 2),
                "session_high": mp.session_high, "session_low": mp.session_low,
                "day_range": mp.day_range,
                "fa_up": mp.fa_up, "fa_dn": mp.fa_dn,
                "ibr_target_pct": round(mp.ibr_target_pct, 2),
            })
    if mp_rows:
        mp_csv = OUTPUT_ROOT / "option_mp_params.csv"
        with open(mp_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(mp_rows[0].keys()))
            w.writeheader(); w.writerows(mp_rows)
        print(f"Wrote option MP params → {mp_csv}")

    # ── 11. Plots ──────────────────────────────────────────────────────────
    _make_plots(results, strat_rows, STRATEGIES, all_ce_mps, all_pe_mps, all_trade_csv)

    print("\nDone.")
    return results


# ── Plot suite ─────────────────────────────────────────────────────────────────
def _make_plots(results, strat_rows, STRATEGIES, all_ce_mps, all_pe_mps, all_trade_csv=None):
    if all_trade_csv is None: all_trade_csv = []
    active = [s for s in STRATEGIES if results[s]["n"] > 0]
    eqs    = [results[s]["eq"] / 1e5 for s in active]
    base_eq = results["baseline_both"]["eq"] / 1e5

    # ── FIGURE 1: Dashboard ───────────────────────────────────────────────
    fig = plt.figure(figsize=(22, 14))
    gs  = gridspec.GridSpec(3, 3, fig, hspace=0.45, wspace=0.35)
    fig.suptitle("Option Market Profile Strategy Sweep — SENSEX Weekly ATM Options",
                 fontsize=14, fontweight="bold", y=0.99)

    # A: Equity bar chart
    ax1 = fig.add_subplot(gs[0, :2])
    short = [s.replace("opt_","").replace("spot_","S:").replace("_"," ") for s in active]
    cols  = ["#27ae60" if eq >= base_eq else "#e74c3c" for eq in eqs]
    bars  = ax1.barh(short, eqs, color=cols, alpha=0.85, edgecolor="white", height=0.6)
    ax1.axvline(base_eq, color="navy", linestyle="--", linewidth=1.5,
                label=f"Baseline ₹{base_eq:.2f}L")
    for bar, strat, eq in zip(bars, active, eqs):
        n   = results[strat]["n"]
        wr  = results[strat]["wr"]
        ax1.text(max(eq, base_eq) + 0.1, bar.get_y() + bar.get_height()/2,
                 f" n={n}  WR={wr:.0f}%  ₹{eq:.2f}L", va="center", fontsize=7.5)
    ax1.set_xlabel("Final Equity (₹ Lakhs)"); ax1.legend(fontsize=9)
    ax1.set_title("Final Equity by Strategy (₹1L start)", fontsize=10, fontweight="bold")
    ax1.set_xlim(0, max(eqs) * 1.35)

    # B: Equity curves top 4
    ax2 = fig.add_subplot(gs[0, 2])
    top4 = sorted(active, key=lambda s: results[s]["eq"], reverse=True)[:4]
    colors4 = ["#e74c3c","#e67e22","#27ae60","#3498db"]
    for strat, col in zip(top4, colors4):
        rows = sorted(strat_rows[strat], key=lambda r: r["entry_time"])
        eq   = 100_000.0; curve = [eq]
        for r in rows:
            eq = eq + eq * r["alloc"] * max(r["blended_return"], FLOOR_PCT) / 100.0
            curve.append(eq)
        lbl = strat.replace("opt_","").replace("_"," ")[:20] + f" ₹{curve[-1]/1e5:.1f}L"
        ax2.plot(curve, color=col, linewidth=1.8, label=lbl)
    ax2.set_title("Equity Curves — Top 4", fontsize=9, fontweight="bold")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"₹{x/1e5:.0f}L"))
    ax2.legend(fontsize=7); ax2.grid(True, alpha=0.3)
    ax2.set_xlabel("Trade #")

    # C: CE option IB extension analysis
    ax3 = fig.add_subplot(gs[1, 0])
    ce_ext = [m for m in all_ce_mps if m.ib_ext_up]
    ce_nxt = [m for m in all_ce_mps if not m.ib_ext_up]
    cats = ["CE IB\nExtended Up", "CE IB\nNot Extended"]
    ext_wr_data = []
    for mps_grp, label in [(ce_ext, "CE IB Extended Up"), (ce_nxt, "CE IB Not Extended")]:
        if mps_grp:
            ext_wr_data.append({
                "label": label,
                "count": len(mps_grp),
                "avg_ext": np.mean([m.ext_up_pct for m in mps_grp if m.ib_ext_up]),
                "day_ibr_ratio": np.mean([m.day_range/m.ibr for m in mps_grp if m.ibr>0]),
            })
    ax3.bar([0, 1],
            [d["day_ibr_ratio"] for d in ext_wr_data],
            color=["#27ae60","#bdc3c7"], alpha=0.85, edgecolor="white")
    ax3.set_xticks([0, 1])
    ax3.set_xticklabels(["IB Extended\n(breakout)", "IB Not Extended\n(range)"], fontsize=8)
    ax3.set_ylabel("Avg Day/IBR Ratio")
    ax3.set_title("CE Option: Day Range as Multiple\nof IB Range", fontsize=9, fontweight="bold")
    for i, d in enumerate(ext_wr_data):
        ax3.text(i, d["day_ibr_ratio"] + 0.02,
                 f"n={d['count']}\n{d['day_ibr_ratio']:.2f}×", ha="center", fontsize=8)

    # D: IBR distribution CE vs PE
    ax4 = fig.add_subplot(gs[1, 1])
    ce_ibrs = [m.ibr for m in all_ce_mps]
    pe_ibrs = [m.ibr for m in all_pe_mps]
    ax4.hist(ce_ibrs, bins=30, color="#3498db", alpha=0.6, label=f"CE IBR (med={np.median(ce_ibrs):.0f})")
    ax4_t = ax4.twinx()
    ax4_t.hist(pe_ibrs, bins=30, color="#e74c3c", alpha=0.6, label=f"PE IBR (med={np.median(pe_ibrs):.0f})")
    ax4.set_xlabel("IB Range (option premium pts)")
    ax4.set_ylabel("CE count", color="#3498db")
    ax4_t.set_ylabel("PE count", color="#e74c3c")
    ax4.set_title("CE vs PE Initial Balance Range\n(option premium)", fontsize=9, fontweight="bold")
    lines1, labels1 = ax4.get_legend_handles_labels()
    lines2, labels2 = ax4_t.get_legend_handles_labels()
    ax4.legend(lines1+lines2, labels1+labels2, fontsize=7, loc="upper right")

    # E: IB extension frequency
    ax5 = fig.add_subplot(gs[1, 2])
    categories = ["CE Ext Up", "CE Ext Dn", "CE FA Up", "CE FA Dn",
                  "PE Ext Up", "PE Ext Dn", "PE FA Up", "PE FA Dn"]
    values = [
        sum(1 for m in all_ce_mps if m.ib_ext_up)  / len(all_ce_mps) * 100,
        sum(1 for m in all_ce_mps if m.ib_ext_dn)  / len(all_ce_mps) * 100,
        sum(1 for m in all_ce_mps if m.fa_up)       / len(all_ce_mps) * 100,
        sum(1 for m in all_ce_mps if m.fa_dn)       / len(all_ce_mps) * 100,
        sum(1 for m in all_pe_mps if m.ib_ext_up)  / len(all_pe_mps) * 100,
        sum(1 for m in all_pe_mps if m.ib_ext_dn)  / len(all_pe_mps) * 100,
        sum(1 for m in all_pe_mps if m.fa_up)       / len(all_pe_mps) * 100,
        sum(1 for m in all_pe_mps if m.fa_dn)       / len(all_pe_mps) * 100,
    ]
    clrs = ["#2ecc71","#e74c3c","#f39c12","#9b59b6"] * 2
    bars5 = ax5.bar(categories, values, color=clrs, alpha=0.85, edgecolor="white")
    for bar, v in zip(bars5, values):
        ax5.text(bar.get_x()+bar.get_width()/2, v+0.5, f"{v:.0f}%", ha="center", fontsize=7)
    ax5.set_ylabel("% of Sessions"); ax5.tick_params(axis="x", labelsize=6.5, rotation=20)
    ax5.set_title("Option IB Extension & FA Frequency", fontsize=9, fontweight="bold")

    # F: Strategy summary table
    ax6 = fig.add_subplot(gs[2, :])
    ax6.axis("off")
    data = [["Strategy", "n", "WR%", "Avg%", "Alloc", "Final ₹L", "+Months", "Key Logic"]]
    alloc_desc = {
        "baseline_both":         "20% flat",
        "opt_ib_ext_ce_only":    "20% flat",
        "opt_ib_ext_pe_only":    "20% flat",
        "opt_ib_ext_alloc":      "35%/10% IB ext",
        "opt_poc_reversion":     "20% flat",
        "opt_poc_alloc":         "35%/10% POC rev",
        "opt_va_break":          "20% flat",
        "opt_ibr_target":        "20% flat",
        "opt_ib_ext_ibr_tgt":    "35%/10% + dyn tgt",
        "opt_ib_ext_poc_combo":  "20% flat",
        "spot_poc_reversion":    "20% flat",
        "spot_poc_alloc":        "35%/10% spot POC",
    }
    logic = {
        "baseline_both":         "All MACD signals",
        "opt_ib_ext_ce_only":    "CE only when opt > IBH",
        "opt_ib_ext_pe_only":    "PE only when opt > IBH",
        "opt_ib_ext_alloc":      "★ Size up when opt IB extended",
        "opt_poc_reversion":     "Opt price below POC (reversion)",
        "opt_poc_alloc":         "Size up when below POC",
        "opt_va_break":          "Opt breaks above VAH",
        "opt_ibr_target":        "IBH+IBR as dynamic exit target",
        "opt_ib_ext_ibr_tgt":    "★★ IB ext sizing + IBR target",
        "opt_ib_ext_poc_combo":  "IB ext AND below POC",
        "spot_poc_reversion":    "Spot below/above spot POC",
        "spot_poc_alloc":        "★ Reference: spot POC sizing",
    }
    for s in STRATEGIES:
        r = results.get(s, {})
        if r.get("n", 0) == 0:
            data.append([s, "0", "—", "—", "—", "—", "—", logic.get(s, "")])
        else:
            data.append([
                s, str(r["n"]),
                f"{r['wr']:.1f}%", f"{r['avg']:+.2f}%",
                alloc_desc.get(s, "20% flat"),
                f"{r['eq']/1e5:.3f}",
                f"{r['pos_m']}/{r['tot_m']}",
                logic.get(s, ""),
            ])

    tbl = ax6.table(cellText=data[1:], colLabels=data[0],
                    loc="center", cellLoc="center")
    tbl.auto_set_font_size(False); tbl.set_fontsize(8)
    tbl.scale(1, 1.5)
    for j in range(len(data[0])):
        tbl[(0, j)].set_facecolor("#2c3e50"); tbl[(0, j)].set_text_props(color="white", fontweight="bold")

    # Highlight best rows
    best_eq = max(r["eq"] for r in results.values() if r.get("n",0)>0)
    for i, s in enumerate(STRATEGIES, 1):
        r = results.get(s, {})
        if r.get("eq", 0) == best_eq:
            for j in range(len(data[0])):
                tbl[(i, j)].set_facecolor("#fef9e7")
                tbl[(i, j)].set_text_props(fontweight="bold")
        elif r.get("eq", 0) and r["eq"] >= results["baseline_both"]["eq"] * 1.5:
            for j in range(len(data[0])):
                tbl[(i, j)].set_facecolor("#eafaf1")

    plt.savefig(OUTPUT_ROOT / "option_mp_dashboard.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Plot → {OUTPUT_ROOT / 'option_mp_dashboard.png'}")

    # ── FIGURE 2: IB Extension deep-dive ─────────────────────────────────
    fig2, axes2 = plt.subplots(2, 2, figsize=(14, 10))
    fig2.suptitle("Option IB Extension — Win Rate & Return Analysis", fontsize=13, fontweight="bold")

    # (0,0) CE: ext vs not ext → blended returns from strategy data
    ext_rows = [r for r in strat_rows["opt_ib_ext_alloc"]
                if r["alloc"] == ALLOC_HIGH]
    nxt_rows = [r for r in strat_rows["opt_ib_ext_alloc"]
                if r["alloc"] == ALLOC_LOW]
    ext_rets = [r["blended_return"] for r in ext_rows]
    nxt_rets = [r["blended_return"] for r in nxt_rows]

    ax = axes2[0, 0]
    ax.hist(ext_rets, bins=25, color="#2ecc71", alpha=0.7, edgecolor="white",
            label=f"IB Extended (n={len(ext_rets)}) WR={sum(1 for r in ext_rets if r>0)/len(ext_rets)*100:.0f}%" if ext_rets else "IB Extended")
    ax.hist(nxt_rets, bins=25, color="#e74c3c", alpha=0.5, edgecolor="white",
            label=f"No Extension (n={len(nxt_rets)}) WR={sum(1 for r in nxt_rets if r>0)/len(nxt_rets)*100:.0f}%" if nxt_rets else "No Extension")
    ax.axvline(0, color="black", linestyle="--")
    ax.set_xlabel("Blended Return %"); ax.set_ylabel("Count")
    ax.set_title("Return Distribution: IB Extended vs Not")
    ax.legend(fontsize=8)

    # (0,1) Equity curve IB ext alloc vs baseline
    ax = axes2[0, 1]
    for strat, col, lw in [("baseline_both","#3498db",1.5), ("opt_ib_ext_alloc","#e74c3c",2.5),
                            ("spot_poc_alloc","#27ae60",2.0), ("opt_ib_ext_ibr_tgt","#e67e22",2.0)]:
        if strat not in results or results[strat]["n"] == 0: continue
        rows_s = sorted(strat_rows[strat], key=lambda r: r["entry_time"])
        eq = 100_000.0; curve = [eq]
        for r in rows_s:
            eq = eq + eq * r["alloc"] * max(r["blended_return"], FLOOR_PCT) / 100.0; curve.append(eq)
        lbl = strat.replace("opt_","opt ").replace("_"," ")[:25] + f" ₹{curve[-1]/1e5:.2f}L"
        ax.plot(curve, color=col, linewidth=lw, label=lbl)
    ax.set_title("Equity Curves: Key Strategies")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"₹{x/1e5:.1f}L"))
    ax.legend(fontsize=8); ax.grid(True, alpha=0.3); ax.set_xlabel("Trade #")

    # (1,0) IBR target distribution
    ax = axes2[1, 0]
    ibr_tgt_rows = [r for r in strat_rows["opt_ib_ext_ibr_tgt"] if "ibr_tgt_pct" in r]
    # Use the opt_ibr_target trades
    ibr_tgt_from_csv = [float(r["ibr_tgt_pct"]) for r in all_trade_csv
                        if r["strategy"] == "opt_ib_ext_ibr_tgt" and r["ibr_tgt_pct"]]
    if ibr_tgt_from_csv:
        ax.hist(ibr_tgt_from_csv, bins=30, color="#9b59b6", alpha=0.8, edgecolor="white")
        ax.axvline(np.median(ibr_tgt_from_csv), color="red", linestyle="--",
                   label=f"Median {np.median(ibr_tgt_from_csv):.1f}%")
        ax.axvline(np.mean(ibr_tgt_from_csv), color="orange", linestyle="-",
                   label=f"Mean {np.mean(ibr_tgt_from_csv):.1f}%")
        ax.axvline(30, color="blue", linestyle=":", linewidth=2, label="30% baseline")
        ax.legend(fontsize=8)
    ax.set_xlabel("IBR-derived Option Target %"); ax.set_ylabel("Count")
    ax.set_title("IBR Target Distribution\n(opt_ib_ext_ibr_tgt strategy)")

    # (1,1) Monthly equity ending values
    ax = axes2[1, 1]
    all_months_list = sorted(set(r["month"] for r in strat_rows["baseline_both"]))
    strats_to_plot  = ["baseline_both", "opt_ib_ext_alloc", "spot_poc_alloc", "opt_ib_ext_ibr_tgt"]
    x = np.arange(len(all_months_list))
    w = 0.20
    month_eqs = {}
    for strat in strats_to_plot:
        if results.get(strat, {}).get("n", 0) == 0: continue
        rows_s = sorted(strat_rows[strat], key=lambda r: r["entry_time"])
        eq = 100_000.0; m_eq = {}
        for r in rows_s:
            eq = eq + eq * r["alloc"] * max(r["blended_return"], FLOOR_PCT) / 100.0
            m_eq[r["month"]] = eq
        month_eqs[strat] = m_eq
    colors_m = ["#3498db","#e74c3c","#27ae60","#e67e22"]
    for i, (strat, col) in enumerate(zip(strats_to_plot, colors_m)):
        if strat not in month_eqs: continue
        vals = [month_eqs[strat].get(m, None) for m in all_months_list]
        vals_plot = [v/1e5 if v else 0 for v in vals]
        lbl = strat.replace("opt_","").replace("_"," ")[:20]
        ax.bar(x + i*w, vals_plot, w, label=lbl, color=col, alpha=0.8)
    ax.set_xticks(x + w*1.5); ax.set_xticklabels([m[5:] for m in all_months_list], rotation=45, fontsize=7)
    ax.set_ylabel("Equity (₹L)"); ax.legend(fontsize=7)
    ax.set_title("Monthly Equity Level — Key Strategies", fontsize=9)

    plt.tight_layout()
    plt.savefig(OUTPUT_ROOT / "option_mp_ib_analysis.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Plot → {OUTPUT_ROOT / 'option_mp_ib_analysis.png'}")


if __name__ == "__main__":
    run()
