"""
Build NIFTY Daily Market Profile + Failure Scores
===================================================
Generates:
  runtime/index_analytics_data/market_profile/underlying=NIFTY/daily_mp_params.csv
  runtime/index_analytics_data/market_profile/underlying=NIFTY/enriched_mp_with_failures.csv

Usage:
  python analysis/build_nifty_mp.py
  python analysis/build_nifty_mp.py --underlying BANKNIFTY --bucket 100
"""
from __future__ import annotations

import argparse
import csv
import gzip
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

DATA_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"


# ── Config (overridden by CLI) ────────────────────────────────────────────────

DEFAULT_UNDERLYING = "NIFTY"
DEFAULT_BUCKET_SIZE = 50   # 50-pt price buckets
TPO_MINUTES = 30
IB_MINUTES = 60
VA_PCT = 0.70
MIN_TPO_PERIODS = 4


# ── Market Profile dataclass ──────────────────────────────────────────────────

@dataclass
class DailyMP:
    date: str
    poc: float
    vah: float
    val: float
    var: float
    ibh: float
    ibl: float
    ibr: float
    ib_broken_up: bool
    ib_broken_dn: bool
    fa_up: bool
    fa_dn: bool
    session_high: float
    session_low: float
    open_price: float
    close_price: float
    total_tpos: int
    tpo_counts: dict = field(default_factory=dict, repr=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bucket(price: float, bucket_size: int) -> float:
    return math.floor(price / bucket_size) * bucket_size


def _load_spot(underlying: str) -> pd.DataFrame:
    path = DATA_ROOT / f"spot/underlying={underlying}/1minute.csv.gz"
    if not path.exists():
        raise FileNotFoundError(f"Spot data not found: {path}")
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ── Daily MP computation ──────────────────────────────────────────────────────

def _compute_daily_mp(day_df: pd.DataFrame, bucket_size: int) -> Optional[DailyMP]:
    day_df = day_df.copy().reset_index(drop=True)
    if len(day_df) < MIN_TPO_PERIODS * TPO_MINUTES:
        return None

    times = day_df["time"]
    date_str = str(times.iloc[0].date())

    # Initial Balance
    ib_mask = (times >= times.iloc[0]) & (times < times.iloc[0] + pd.Timedelta(minutes=IB_MINUTES))
    ib_df = day_df[ib_mask]
    if ib_df.empty:
        return None
    ibh = float(ib_df["high"].max())
    ibl = float(ib_df["low"].min())
    ibr = ibh - ibl

    # TPO counts
    tpo_counts: dict[float, int] = defaultdict(int)
    session_start = times.iloc[0]
    for i, row in day_df.iterrows():
        period_idx = int((row["time"] - session_start).total_seconds() // (TPO_MINUTES * 60))
        # Assign all prices in this candle's range to the TPO
        lo = _bucket(float(row["low"]), bucket_size)
        hi = float(row["high"])
        b = lo
        while b <= hi:
            tpo_counts[b] += 1
            b += bucket_size

    if not tpo_counts:
        return None

    # POC
    poc = max(tpo_counts, key=tpo_counts.__getitem__)
    total_tpos = sum(tpo_counts.values())

    # Value Area (70%)
    sorted_buckets = sorted(tpo_counts.keys(), key=lambda b: -tpo_counts[b])
    va_target = total_tpos * VA_PCT
    va_accum = 0
    va_buckets = set()
    for b in sorted_buckets:
        va_accum += tpo_counts[b]
        va_buckets.add(b)
        if va_accum >= va_target:
            break

    vah = max(va_buckets) + bucket_size
    val = min(va_buckets)

    # Session stats
    session_high = float(day_df["high"].max())
    session_low = float(day_df["low"].min())
    open_price = float(day_df["open"].iloc[0])
    close_price = float(day_df["close"].iloc[-1])

    # IB break and Failed Auction
    post_ib = day_df[~ib_mask]
    ib_broken_up = bool((post_ib["high"] > ibh).any()) if not post_ib.empty else False
    ib_broken_dn = bool((post_ib["low"] < ibl).any()) if not post_ib.empty else False

    fa_up = ib_broken_up and (close_price < ibh)
    fa_dn = ib_broken_dn and (close_price > ibl)

    return DailyMP(
        date=date_str,
        poc=poc,
        vah=vah,
        val=val,
        var=vah - val,
        ibh=ibh,
        ibl=ibl,
        ibr=ibr,
        ib_broken_up=ib_broken_up,
        ib_broken_dn=ib_broken_dn,
        fa_up=fa_up,
        fa_dn=fa_dn,
        session_high=session_high,
        session_low=session_low,
        open_price=open_price,
        close_price=close_price,
        total_tpos=total_tpos,
        tpo_counts=dict(tpo_counts),
    )


# ── Failure score computation ─────────────────────────────────────────────────

def _compute_failure_scores(
    mp_df: pd.DataFrame,
    spot_df: pd.DataFrame,
    bucket_size: int,
) -> pd.DataFrame:
    """Compute buyer/seller failure scores and day-type labels."""

    spot_df = spot_df.copy()
    spot_df["date"] = spot_df["time"].dt.date

    rows = []
    mp_list = mp_df.to_dict("records")

    for i, row in enumerate(mp_list):
        d = row.copy()

        # Get today's 1-min data
        dt = pd.to_datetime(row["date"]).date()
        day1m = spot_df[spot_df["date"] == dt].sort_values("time")

        poc = float(row["poc"])
        vah = float(row["vah"])
        val = float(row["val"])
        ibh = float(row["ibh"])
        ibl = float(row["ibl"])
        ibr = float(row["ibr"]) if row["ibr"] else 1.0
        close = float(row["close_price"])
        session_high = float(row["session_high"])
        session_low = float(row["session_low"])

        # ── Poor High / Poor Low (single-TPO extreme) ──────────────────────
        # Approximate: extreme bucket has only 1 TPO count (no acceptance)
        poor_high = session_high - vah < bucket_size * 1.5
        poor_low = val - session_low < bucket_size * 1.5

        # ── Excess tail (strong rejection at extreme) ──────────────────────
        excess_high = session_high - vah > ibr * 0.5
        excess_low = val - session_low > ibr * 0.5

        # Tail TPO count (rough approximation)
        tail_high_buckets = max(0, round((session_high - vah) / bucket_size))
        tail_low_buckets = max(0, round((val - session_low) / bucket_size))

        # ── IB Extension Failure ──────────────────────────────────────────
        ib_ext_up_fail = bool(row.get("ib_broken_up")) and (close < ibh)
        ib_ext_dn_fail = bool(row.get("ib_broken_dn")) and (close > ibl)

        # ── IB Extension + Reversal ───────────────────────────────────────
        ib_ext_up_reversal = bool(row.get("ib_broken_up")) and (close < poc)
        ib_ext_dn_reversal = bool(row.get("ib_broken_dn")) and (close > poc)

        # ── Close position ────────────────────────────────────────────────
        close_pct_range = (
            (close - session_low) / (session_high - session_low)
            if (session_high - session_low) > 0
            else 0.5
        )

        # ── Buyer Failure Score ───────────────────────────────────────────
        # Higher = more evidence buyers are failing → bearish
        buyer_fail = 0
        if row.get("fa_up"):
            buyer_fail += 2   # Strong signal
        if ib_ext_up_fail:
            buyer_fail += 2
        if poor_high and not excess_high:
            buyer_fail += 1   # No acceptance at high
        if close_pct_range < 0.35:
            buyer_fail += 1   # Close in lower 35% of range
        if ib_ext_up_reversal:
            buyer_fail += 1

        # ── Seller Failure Score ──────────────────────────────────────────
        # Higher = more evidence sellers are failing → bullish
        seller_fail = 0
        if row.get("fa_dn"):
            seller_fail += 2
        if ib_ext_dn_fail:
            seller_fail += 2
        if poor_low and not excess_low:
            seller_fail += 1
        if close_pct_range > 0.65:
            seller_fail += 1
        if ib_ext_dn_reversal:
            seller_fail += 1

        net_failure = seller_fail - buyer_fail

        # ── Day Type ──────────────────────────────────────────────────────
        ib_ext_up = bool(row.get("ib_broken_up"))
        ib_ext_dn = bool(row.get("ib_broken_dn"))
        fa_up = bool(row.get("fa_up"))
        fa_dn = bool(row.get("fa_dn"))
        daily_move = close - float(row["open_price"])
        ibr_val = max(ibr, 1.0)
        daily_move_in_ibs = abs(daily_move) / ibr_val

        if fa_up or fa_dn:
            day_type = "FAILED_AUCTION"
        elif ib_ext_up and not ib_ext_dn and daily_move > ibr_val * 0.8:
            day_type = "TREND_UP"
        elif ib_ext_dn and not ib_ext_up and daily_move < -ibr_val * 0.8:
            day_type = "TREND_DN"
        elif ib_ext_up and not ib_ext_dn:
            day_type = "NORMAL_VAR_UP"
        elif ib_ext_dn and not ib_ext_up:
            day_type = "NORMAL_VAR_DN"
        elif ib_ext_up and ib_ext_dn:
            day_type = "DOUBLE_DIST"
        else:
            day_type = "NORMAL"

        # ── Next day move ─────────────────────────────────────────────────
        next_close = None
        next_3d_close = None
        if i + 1 < len(mp_list):
            next_close = float(mp_list[i + 1]["close_price"])
        if i + 3 < len(mp_list):
            next_3d_close = float(mp_list[i + 3]["close_price"])

        next_day_move = (next_close - close) if next_close is not None else None
        next_3d_move = (next_3d_close - close) if next_3d_close is not None else None

        d.update({
            "daily_move": daily_move,
            "daily_pct": daily_move / float(row["open_price"]) * 100,
            "close_pct_range": close_pct_range,
            "day_type": day_type,
            "fa_up": row.get("fa_up"),
            "fa_dn": row.get("fa_dn"),
            "ib_broken_up": row.get("ib_broken_up"),
            "ib_broken_dn": row.get("ib_broken_dn"),
            "ib_ext_up_fail": ib_ext_up_fail,
            "ib_ext_dn_fail": ib_ext_dn_fail,
            "ib_ext_up_reversal": ib_ext_up_reversal,
            "ib_ext_dn_reversal": ib_ext_dn_reversal,
            "poor_high": poor_high,
            "poor_low": poor_low,
            "excess_high": excess_high,
            "excess_low": excess_low,
            "tail_high_buckets": tail_high_buckets,
            "tail_low_buckets": tail_low_buckets,
            "buyer_fail_score": buyer_fail,
            "seller_fail_score": seller_fail,
            "net_failure": net_failure,
            "next_day_move": next_day_move,
            "next_3d_move": next_3d_move,
        })
        rows.append(d)

    return pd.DataFrame(rows)


# ── Day type statistics ───────────────────────────────────────────────────────

def _print_stats(enriched: pd.DataFrame, underlying: str):
    print(f"\n{'='*60}")
    print(f"  {underlying} Market Profile — Summary Statistics")
    print(f"{'='*60}")
    print(f"  Total sessions  : {len(enriched)}")
    print(f"  Date range      : {enriched['date'].min()} to {enriched['date'].max()}")
    print()

    day_type_counts = enriched["day_type"].value_counts()
    print("  Day Types:")
    for dt, cnt in day_type_counts.items():
        print(f"    {dt:<20} {cnt:>4}  ({cnt/len(enriched)*100:.1f}%)")

    print()
    fa_up = enriched["fa_up"].astype(str).eq("True").sum()
    fa_dn = enriched["fa_dn"].astype(str).eq("True").sum()
    print(f"  Failed Auction Up  : {fa_up}")
    print(f"  Failed Auction Dn  : {fa_dn}")

    print()
    print(f"  Avg buyer_fail_score : {enriched['buyer_fail_score'].mean():.2f}")
    print(f"  Avg seller_fail_score: {enriched['seller_fail_score'].mean():.2f}")

    # Next day direction accuracy
    valid = enriched.dropna(subset=["next_day_move"])
    if len(valid) > 0:
        seller_fail_high = valid[valid["seller_fail_score"] >= 3]
        buyer_fail_high = valid[valid["buyer_fail_score"] >= 3]
        if len(seller_fail_high) > 0:
            pct_up = (seller_fail_high["next_day_move"] > 0).mean() * 100
            print(f"\n  Seller_fail≥3 → next day UP: {pct_up:.1f}% ({len(seller_fail_high)} cases)")
        if len(buyer_fail_high) > 0:
            pct_dn = (buyer_fail_high["next_day_move"] < 0).mean() * 100
            print(f"  Buyer_fail≥3  → next day DN: {pct_dn:.1f}% ({len(buyer_fail_high)} cases)")


# ── Main ──────────────────────────────────────────────────────────────────────

def build_mp(underlying: str, bucket_size: int):
    print(f"\n[1] Loading {underlying} 1-min spot data...")
    spot_df = _load_spot(underlying)
    spot_df["date"] = spot_df["time"].dt.date
    print(f"    {len(spot_df)} 1-min candles loaded")

    print(f"\n[2] Computing daily Market Profile (bucket={bucket_size}pt)...")
    daily_mp_all: dict[str, DailyMP] = {}
    for dt, grp in spot_df.groupby("date"):
        mp = _compute_daily_mp(grp, bucket_size)
        if mp:
            daily_mp_all[str(dt)] = mp
    print(f"    {len(daily_mp_all)} sessions computed")

    # Save daily_mp_params.csv
    out_dir = DATA_ROOT / "market_profile" / f"underlying={underlying}"
    out_dir.mkdir(parents=True, exist_ok=True)

    mp_rows = []
    for mp in sorted(daily_mp_all.values(), key=lambda x: x.date):
        mp_rows.append({
            "date": mp.date,
            "poc": mp.poc,
            "vah": mp.vah,
            "val": mp.val,
            "var": mp.var,
            "ibh": mp.ibh,
            "ibl": mp.ibl,
            "ibr": mp.ibr,
            "ib_broken_up": mp.ib_broken_up,
            "ib_broken_dn": mp.ib_broken_dn,
            "fa_up": mp.fa_up,
            "fa_dn": mp.fa_dn,
            "session_high": mp.session_high,
            "session_low": mp.session_low,
            "open_price": mp.open_price,
            "close_price": mp.close_price,
            "total_tpos": mp.total_tpos,
        })

    mp_df = pd.DataFrame(mp_rows)
    mp_path = out_dir / "daily_mp_params.csv"
    mp_df.to_csv(mp_path, index=False)
    print(f"    Saved: {mp_path}")

    print(f"\n[3] Computing failure scores and day types...")
    enriched = _compute_failure_scores(mp_df, spot_df, bucket_size)

    enriched_path = out_dir / "enriched_mp_with_failures.csv"
    enriched.to_csv(enriched_path, index=False)
    print(f"    Saved: {enriched_path}")

    _print_stats(enriched, underlying)

    return enriched


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying", default=DEFAULT_UNDERLYING)
    parser.add_argument("--bucket", type=int, default=DEFAULT_BUCKET_SIZE)
    args = parser.parse_args()

    build_mp(args.underlying, args.bucket)
