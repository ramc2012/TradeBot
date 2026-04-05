"""
Staggered Exit Strategy — Index Option MACD Zero-Cross

Strategy:
  - Entry  : MACD(12,26,9) bullish zero-line cross on 5-min ATM CE + PE candles
  - Hold   : full position until return >= 50%
  - Ladder : once >= 50%, exit 10% of original position at each +10% level (50,60,70,…)
  - Trail  : 10% trailing stop on *remaining* position (from candle high)
  - Hard SL: −50% at any point (options can expire worthless)

Compares:
  1. staggered_50_trail10   — staggered from 50%, trail 10%
  2. target_30pct           — baseline (fixed 30% target, best from timeframe sweep)
  3. target_50pct           — fixed 50% target
  4. target_75pct           — fixed 75% target

Output: runtime/index_analytics_data/staggered_exit/
"""
from __future__ import annotations

import csv, gzip, json, os, sys
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_ROOT   = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"
OUTPUT_ROOT = DATA_ROOT / "staggered_exit"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

TIMEFRAME     = "5m"
TIMEFRAME_FREQ = "5min"
MIN_CANDLES   = 40
HARD_SL_PCT   = -30.0   # hard stop: exit ALL at -30%

# ── MACD engine ────────────────────────────────────────────────────────────────
def _ema(values: list[float], period: int) -> list[Optional[float]]:
    n = len(values); result: list[Optional[float]] = [None] * n
    if n < period: return result
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    k = 2.0 / (period + 1); prev = sma
    for i in range(period, n):
        v = values[i] * k + prev * (1.0 - k)
        result[i] = v; prev = v
    return result

def _macd(closes: list[float]):
    n = len(closes)
    ef = _ema(closes, 12); es = _ema(closes, 26)
    ml = [(ef[i] - es[i]) if ef[i] is not None and es[i] is not None else None for i in range(n)]
    fv = next((i for i, v in enumerate(ml) if v is not None), -1)
    sl = [None] * n
    if fv == -1: return ml, sl
    vm = [ml[i] for i in range(fv, n)]
    es2 = _ema(vm, 9)  # type: ignore
    for j, v in enumerate(es2):
        sl[fv + j] = v
    return ml, sl

# ── Data loading ───────────────────────────────────────────────────────────────
@lru_cache(maxsize=512)
def _load_1m(path_str: str) -> pd.DataFrame:
    path = DATA_ROOT / path_str
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for col in ("open","high","low","close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

@lru_cache(maxsize=512)
def _resample5(path_str: str) -> pd.DataFrame:
    df = _load_1m(path_str)
    agg = {"open":"first","high":"max","low":"min","close":"last","volume":"sum","oi":"last"}
    agg = {k:v for k,v in agg.items() if k in df.columns}
    r = df.set_index("time").resample(TIMEFRAME_FREQ, label="right", closed="right").agg(agg).dropna(subset=["open","close"]).reset_index()
    return r

@lru_cache(maxsize=4)
def _spot(underlying: str) -> pd.DataFrame:
    return _load_1m(f"spot/underlying={underlying}/1minute.csv.gz")

def _spot_at(underlying: str, ts) -> Optional[float]:
    s = _spot(underlying).set_index("time").sort_index()
    b = s.loc[:ts]
    if not b.empty: return float(b.iloc[-1]["close"])
    a = s.loc[ts:]
    if not a.empty: return float(a.iloc[0]["close"])
    return None

# ── Series descriptors ─────────────────────────────────────────────────────────
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
             m.get("option_type") and m.get("expiry_kind") in kinds]

    by_group: dict[tuple, list] = defaultdict(list)
    for m in metas:
        by_group[(m["underlying"], m["expiry_kind"], m["expiry"])].append(m)

    descs: list[Desc] = []
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
        spot = _spot_at(und, min(p for _, p, _, _ in candidates))
        if spot is None: continue
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


# ── Exit simulators ────────────────────────────────────────────────────────────

def _sim_fixed_target(candles, entry_idx: int, target_pct: float) -> dict:
    """Fixed-target exit — NO intra-trade hard SL. Raw return recorded; -50% floor
    applied during compounding only (matches original timeframe_sweep behaviour)."""
    ep = float(candles[entry_idx]["close"])
    target_px = ep * (1.0 + target_pct / 100.0)
    for i in range(entry_idx + 1, len(candles)):
        hi = float(candles[i]["high"])
        if hi >= target_px:
            return {"exit_idx": i, "exit_price": target_px,
                    "exit_time": str(candles[i]["time"]),
                    "exit_reason": f"target_{int(target_pct)}pct_hit",
                    "blended_return": target_pct}
    i = len(candles) - 1
    cl = float(candles[i]["close"])
    return {"exit_idx": i, "exit_price": cl,
            "exit_time": str(candles[i]["time"]),
            "exit_reason": "hold_to_end",
            "blended_return": round((cl - ep) / ep * 100.0, 4)}


def _sim_staggered(candles, entry_idx: int,
                   ladder_start_pct: float = 50.0,
                   ladder_step_pct: float  = 10.0,
                   trail_return_pts: float = 10.0) -> dict:
    """
    Correct staggered exit:

      Phase 1 — before reaching +50%:
        • Hard stop at −50%: if close <= entry*(1−0.50), exit 100% at −50%.
        • Otherwise hold.

      Phase 2 — once +30% is breached (candle high >= entry*1.30):
        • Immediately exit 30% of position at +30%.
        • For each subsequent +10% level (40, 50, 60 …): exit 10% of ORIGINAL position
          at that level price.
        • Trailing stop on remaining (in return-point terms):
          if current_return_pct < (peak_return_pct_since_30 − trail_return_pts),
          exit remaining at the trigger price (intrabar).
        • Hold to series end if neither triggered.

    peak_return_pct tracks the highest return (based on candle highs) ever seen
    after the first ladder exit, updated bar by bar.

    Example: peak at +78%, falls to +68% (78−10=68) → remaining killed.
    First exit: 30% of position at +30%. Then 10% per 10%: totals 100% by +100%.

    Blended return = sum of (fraction_exited × return_at_that_level).
    """
    ep = float(candles[entry_idx]["close"])
    if ep <= 0.0:
        return {"exit_idx": entry_idx, "exit_price": ep,
                "exit_time": str(candles[entry_idx]["time"]),
                "exit_reason": "zero_price", "blended_return": 0.0,
                "ladder_exits": [], "remaining_at_exit": 1.0}

    hard_sl_px   = ep * (1.0 + HARD_SL_PCT / 100.0)   # ep * 0.50
    remaining    = 1.0
    realized_pct = 0.0
    ladder_active = False
    # next level to trigger a partial exit (starts at 50; first exit is 50% fraction)
    next_level   = ladder_start_pct
    peak_ret_pct = 0.0          # highest return-pct seen after ladder activates
    exited_steps: list[float] = []

    for i in range(entry_idx + 1, len(candles)):
        if remaining <= 1e-9:
            break
        c  = candles[i]
        hi = float(c["high"])
        lo = float(c["low"])
        cl = float(c["close"])
        ret_hi = (hi - ep) / ep * 100.0
        ret_cl = (cl - ep) / ep * 100.0

        # ── Process HIGH first: ladder exits ────────────────────────────────
        # (candle high is tested before low — if both +30% and −30% happen
        #  in the same bar, the upside move is processed first, then the trail/SL)
        while remaining > 1e-9 and ret_hi >= next_level:
            if not ladder_active:
                # First exit: 30% of position at +30%
                fraction      = 0.30
                ladder_active = True
            else:
                # Subsequent exits: 10% of ORIGINAL position per +10% level
                fraction = min(0.10, remaining)
            realized_pct += fraction * next_level
            remaining    -= fraction
            exited_steps.append(round(next_level, 1))
            next_level   += ladder_step_pct

        # Update peak return after ladder activation (based on this bar's high)
        if ladder_active:
            if ret_hi > peak_ret_pct:
                peak_ret_pct = ret_hi

        # ── Process LOW: hard SL (only if ladder still not active) ──────────
        if not ladder_active:
            if lo <= hard_sl_px:
                realized_pct += remaining * HARD_SL_PCT
                return {"exit_idx": i, "exit_price": round(hard_sl_px, 4),
                        "exit_time": str(c["time"]), "exit_reason": "hard_sl",
                        "blended_return": round(realized_pct, 4),
                        "ladder_exits": exited_steps,
                        "remaining_at_exit": round(remaining, 4)}

        if remaining <= 1e-9:
            return {"exit_idx": i, "exit_price": round(ep * (1 + (next_level - ladder_step_pct) / 100.0), 4),
                    "exit_time": str(c["time"]), "exit_reason": "fully_laddered",
                    "blended_return": round(realized_pct, 4),
                    "ladder_exits": exited_steps, "remaining_at_exit": 0.0}

        # ── Trailing stop (only after ladder activated) ────────────────────────
        if ladder_active:
            trail_trigger_ret = peak_ret_pct - trail_return_pts
            trail_trigger_px  = ep * (1.0 + trail_trigger_ret / 100.0)
            lo = float(c["low"])
            if lo <= trail_trigger_px:
                # Exit at the trigger price (intrabar), same precision as target exits
                exit_ret = trail_trigger_ret
                realized_pct += remaining * exit_ret
                return {"exit_idx": i, "exit_price": round(trail_trigger_px, 4),
                        "exit_time": str(c["time"]), "exit_reason": "trail_stop",
                        "blended_return": round(realized_pct, 4),
                        "ladder_exits": exited_steps,
                        "remaining_at_exit": round(remaining, 4)}

    # Series end
    i  = len(candles) - 1
    cl = float(candles[i]["close"])
    r  = (cl - ep) / ep * 100.0
    realized_pct += remaining * r
    return {"exit_idx": i, "exit_price": round(cl, 4),
            "exit_time": str(candles[i]["time"]), "exit_reason": "hold_to_end",
            "blended_return": round(realized_pct, 4),
            "ladder_exits": exited_steps,
            "remaining_at_exit": round(remaining, 4)}


def _max_possible(candles, entry_idx: int) -> float:
    ep = float(candles[entry_idx]["close"])
    if ep <= 0: return 0.0
    mp = max(float(c["high"]) for c in candles[entry_idx:])
    return round((mp - ep) / ep * 100.0, 4)


# ── Main loop ──────────────────────────────────────────────────────────────────

STRATEGIES = {
    "staggered_30_sl30_trail10": lambda c, i: _sim_staggered(c, i, 30.0, 10.0, 10.0),
    "target_30pct":              lambda c, i: _sim_fixed_target(c, i, 30.0),
    "target_50pct":              lambda c, i: _sim_fixed_target(c, i, 50.0),
    "target_75pct":              lambda c, i: _sim_fixed_target(c, i, 75.0),
}

def _scan_entries(candles, ml, advance_sim_fn) -> list[int]:
    """
    Find all MACD zero-cross entry indices using a shared advance cursor.
    advance_sim_fn defines how far to advance after each entry (used to
    avoid re-entering during an open trade).  We use target_30pct as the
    shared advance so all strategies see the same entry opportunities.
    """
    entries: list[int] = []
    idx = 1
    while idx < len(candles):
        prev = ml[idx - 1]; curr = ml[idx]
        if prev is None or curr is None or not (prev <= 0.0 and curr > 0.0):
            idx += 1; continue
        ep = float(candles[idx]["close"])
        if ep <= 0.0:
            idx += 1; continue
        entries.append(idx)
        # Advance past the shared exit (target_30pct — no early re-entry)
        res = advance_sim_fn(candles, idx)
        idx = int(res["exit_idx"]) + 1
    return entries


def run():
    print("Building series descriptors …")
    descs = _build_descs(kinds=("weekly",))
    print(f"  Found {len(descs)} weekly series")

    all_trades: list[dict] = []
    # Shared advance uses target_30pct (no hard SL, no re-entry within a trade)
    advance_fn = lambda c, i: _sim_fixed_target(c, i, 30.0)

    for di, desc in enumerate(descs):
        if di % 5 == 0:
            print(f"  [{di+1}/{len(descs)}] {desc.series_id}")

        for opt_type, path in (("CE", desc.ce_path), ("PE", desc.pe_path)):
            try:
                frame = _resample5(path)
            except FileNotFoundError:
                continue
            frame = frame[frame["time"] >= pd.Timestamp(desc.pair_start)].copy().reset_index(drop=True)
            if len(frame) < MIN_CANDLES:
                continue

            candles = frame.to_dict("records")
            closes  = [float(c["close"]) for c in candles]
            ml, _   = _macd(closes)

            # Same entry points for ALL strategies
            entries = _scan_entries(candles, ml, advance_fn)
            if not entries:
                continue

            for entry_idx in entries:
                ep = float(candles[entry_idx]["close"])
                mp = _max_possible(candles, entry_idx)

                for strat_name, sim_fn in STRATEGIES.items():
                    res = sim_fn(candles, entry_idx)
                    all_trades.append({
                        "strategy":            strat_name,
                        "underlying":          desc.underlying,
                        "expiry_kind":         desc.expiry_kind,
                        "expiry":              desc.expiry,
                        "option_type":         opt_type,
                        "entry_time":          str(candles[entry_idx]["time"]),
                        "entry_price":         round(ep, 4),
                        "exit_time":           res["exit_time"],
                        "exit_price":          round(float(res["exit_price"]), 4),
                        "exit_reason":         res["exit_reason"],
                        "blended_return":      res["blended_return"],
                        "max_possible_return": mp,
                        "ladder_exits":        "|".join(str(x) for x in res.get("ladder_exits", [])),
                        "remaining_at_exit":   res.get("remaining_at_exit", 0.0),
                    })

    # ── Write trades CSV ───────────────────────────────────────────────────────
    out_path = OUTPUT_ROOT / "trade_results.csv"
    if all_trades:
        fieldnames = list(all_trades[0].keys())
        with open(out_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader(); w.writerows(all_trades)
    print(f"\nWrote {len(all_trades)} rows → {out_path}")

    # ── Aggregate stats ────────────────────────────────────────────────────────
    import statistics as st

    df = pd.DataFrame(all_trades)
    print("\n" + "="*70)
    print(f"{'Strategy':<32} {'n':>5} {'WR%':>6} {'AvgRet%':>9} {'MedRet%':>9} {'EV':>8}")
    print("="*70)

    for strat in STRATEGIES:
        t = df[df["strategy"]==strat]
        n = len(t)
        if n == 0: continue
        wr  = (t["blended_return"] > 0).sum() / n * 100
        avg = t["blended_return"].mean()
        med = t["blended_return"].median()
        aw  = t[t["blended_return"]>0]["blended_return"].mean()
        al  = t[t["blended_return"]<=0]["blended_return"].mean()
        ev  = wr/100*aw + (1-wr/100)*al
        print(f"  {strat:<30} {n:>5} {wr:>6.1f} {avg:>+9.2f} {med:>+9.2f} {ev:>+8.2f}")

    # ── Compounded equity ──────────────────────────────────────────────────────
    # Staggered uses -30% floor (hard SL is -30%), fixed targets use -50% floor
    floors = {"staggered_30_sl30_trail10": -30.0,
              "target_30pct": -50.0, "target_50pct": -50.0, "target_75pct": -50.0}
    print("\n=== Compounded equity (20% alloc, ₹1L start) ===")
    for strat in STRATEGIES:
        t   = df[df["strategy"]==strat].sort_values("entry_time")
        fl  = floors.get(strat, -50.0)
        eq  = 100_000.0
        for r in t["blended_return"]:
            r  = max(r, fl)
            eq = eq + eq * 0.20 * r / 100.0
        pnl = (eq - 100_000) / 100_000 * 100
        print(f"  {strat:<32} (floor {fl:+.0f}%)  → ₹{eq/1e5:.3f}L  ({pnl:+.1f}%)")

    # ── Per-month win rate for staggered ──────────────────────────────────────
    STAG_KEY = "staggered_30_sl30_trail10"
    print(f"\n=== Monthly breakdown — {STAG_KEY} ===")
    ts = df[df["strategy"]==STAG_KEY].copy()
    ts["month"] = pd.to_datetime(ts["entry_time"]).dt.to_period("M").astype(str)
    monthly = ts.groupby("month").agg(
        n=("blended_return","count"),
        wr=("blended_return", lambda x: (x>0).sum()/len(x)*100),
        avg=("blended_return","mean"),
        med=("blended_return","median")
    ).reset_index()
    pos_months = (monthly["avg"] > 0).sum()
    print(f"  Positive months: {pos_months}/{len(monthly)}")
    for _, r in monthly.iterrows():
        print(f"  {r['month']}  n={int(r['n']):3d}  WR={r['wr']:5.1f}%  avg={r['avg']:+.1f}%  med={r['med']:+.1f}%")

    print("\nDone.")

if __name__ == "__main__":
    run()
