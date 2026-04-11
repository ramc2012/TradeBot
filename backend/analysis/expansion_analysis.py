"""
Expansion & Overnight Carry Analysis — SENSEX Weekly Options
=============================================================

Uses existing daily MP params (spot) and option MP params to:
  1. Classify each trading day as a Market Profile day type
  2. Measure overnight gap contributions to daily moves
  3. Detect multi-day Value Area migrations (expansion phases)
  4. Score multi-timeframe expansion confluence signals
  5. Backtest an overnight carry strategy on SENSEX ATM options

Reuses: daily_mp_params.csv, option_mp_params.csv, 1-min spot/option data
Output: runtime/index_analytics_data/expansion/
"""
from __future__ import annotations

import csv, gzip, json, math, os
from collections import defaultdict
from dataclasses import dataclass, field
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

UNDERLYING = "SENSEX"

# ── Helpers ───────────────────────────────────────────────────────────────────
def _pct(a, b):
    """Percentage change from b to a."""
    if b == 0: return 0.0
    return (a - b) / abs(b) * 100.0


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — LOAD EXISTING MP DATA
# ═══════════════════════════════════════════════════════════════════════════════

def _load_daily_mp() -> pd.DataFrame:
    """Load spot-based daily MP params."""
    path = DATA_ROOT / "market_profile" / "daily_mp_params.csv"
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("poc", "vah", "val", "var", "ibh", "ibl", "ibr",
              "session_high", "session_low", "open_price", "close_price"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    return df


def _load_option_mp() -> pd.DataFrame:
    """Load option-based daily MP params."""
    path = DATA_ROOT / "option_mp" / "option_mp_params.csv"
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("poc", "vah", "val", "ibh", "ibl", "ibr", "ib_open",
              "session_high", "session_low", "day_range", "ibr_target_pct",
              "ext_up_pct", "ext_dn_pct"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@lru_cache(maxsize=1)
def _load_spot_1m() -> pd.DataFrame:
    """Load 1-minute SENSEX spot candles."""
    path = DATA_ROOT / f"spot/underlying={UNDERLYING}/1minute.csv.gz"
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — DAY-TYPE CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

def classify_days(mp: pd.DataFrame) -> pd.DataFrame:
    """
    Classify each trading day using Market Profile day types:

    TREND DAY:
      - IB broken in one direction only (up or down, not both)
      - Session range > 2× IBR  (big extension beyond IB)
      - Close in extreme 25% of range (near high or low)

    NORMAL DAY:
      - IB NOT broken in either direction
      - Session range ≈ IBR (no extension)
      - Close near middle of range

    NORMAL VARIATION:
      - IB broken in one direction but modestly
      - Session range 1.2×–2× IBR
      - Close not at extreme

    DOUBLE DISTRIBUTION:
      - IB broken in BOTH directions
      - Wide range
      - Two value areas (approximated by VA range being large relative to session range)

    NEUTRAL / INSIDE DAY:
      - Very small range relative to previous day
      - IB not broken
    """
    rows = []
    for i, r in mp.iterrows():
        session_range = r["session_high"] - r["session_low"]
        ibr = r["ibr"]
        ib_up = r["ib_broken_up"]
        ib_dn = r["ib_broken_dn"]
        fa_up = r["fa_up"]
        fa_dn = r["fa_dn"]
        o = r["open_price"]
        c = r["close_price"]
        h = r["session_high"]
        l = r["session_low"]

        if ibr <= 0 or session_range <= 0:
            rows.append("UNKNOWN")
            continue

        range_ratio = session_range / ibr
        # Close position: 0 = at low, 1 = at high
        close_pos = (c - l) / session_range if session_range > 0 else 0.5
        # Open position
        open_pos = (o - l) / session_range if session_range > 0 else 0.5

        # ── Trend Day ────────────────────────────────────────────────────
        # Strong one-directional move: one-sided IB break, big range, close at extreme
        if (ib_up != ib_dn) and range_ratio >= 2.0 and \
           ((ib_up and close_pos >= 0.70) or (ib_dn and close_pos <= 0.30)):
            if ib_up:
                rows.append("TREND_UP")
            else:
                rows.append("TREND_DN")
            continue

        # ── Double Distribution ──────────────────────────────────────────
        # Both sides broken, wide range
        if ib_up and ib_dn and range_ratio >= 1.5:
            rows.append("DOUBLE_DIST")
            continue

        # ── Normal Variation ─────────────────────────────────────────────
        # One side broken but not a full trend day
        if (ib_up != ib_dn) and range_ratio >= 1.2:
            if ib_up:
                rows.append("NORMAL_VAR_UP")
            else:
                rows.append("NORMAL_VAR_DN")
            continue

        # ── Failed Auction (reversal) ────────────────────────────────────
        if fa_up or fa_dn:
            rows.append("FAILED_AUCTION")
            continue

        # ── Normal / Inside Day ──────────────────────────────────────────
        if range_ratio < 1.2:
            rows.append("NORMAL")
        else:
            rows.append("NORMAL")

    mp = mp.copy()
    mp["day_type"] = rows

    # Directional bias
    mp["direction"] = np.where(mp["close_price"] > mp["open_price"], "UP", "DN")

    # Close position within range
    mp["close_pct"] = np.where(
        (mp["session_high"] - mp["session_low"]) > 0,
        (mp["close_price"] - mp["session_low"]) / (mp["session_high"] - mp["session_low"]),
        0.5
    )

    # Range ratio
    mp["range_ratio"] = np.where(mp["ibr"] > 0, (mp["session_high"] - mp["session_low"]) / mp["ibr"], 1.0)

    return mp


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — OVERNIGHT GAP ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def compute_overnight_gaps(mp: pd.DataFrame) -> pd.DataFrame:
    """
    For each day, compute:
      - gap_pts: today's open - yesterday's close (points)
      - gap_pct: gap as % of yesterday's close
      - gap_direction: UP or DN
      - gap_vs_ib: gap as % of today's IBR
      - gap_contribution: how much of today's total range was the gap
      - gap_filled: did today's session fill the gap?
      - day_return_pts: today's close - today's open
      - overnight_return_pts: today's open - yesterday's close
    """
    mp = mp.copy()
    mp["prev_close"] = mp["close_price"].shift(1)
    mp["prev_high"] = mp["session_high"].shift(1)
    mp["prev_low"] = mp["session_low"].shift(1)
    mp["prev_poc"] = mp["poc"].shift(1)
    mp["prev_vah"] = mp["vah"].shift(1)
    mp["prev_val"] = mp["val"].shift(1)

    # Gap calculations
    mp["gap_pts"] = mp["open_price"] - mp["prev_close"]
    mp["gap_pct"] = mp["gap_pts"] / mp["prev_close"] * 100.0
    mp["gap_abs_pct"] = mp["gap_pct"].abs()
    mp["gap_direction"] = np.where(mp["gap_pts"] > 0, "UP", "DN")

    # Gap relative to today's IB
    mp["gap_vs_ibr"] = np.where(mp["ibr"] > 0, mp["gap_pts"].abs() / mp["ibr"], 0.0)

    # Gap contribution to total day range
    session_range = mp["session_high"] - mp["session_low"]
    mp["gap_contribution"] = np.where(
        session_range > 0,
        mp["gap_pts"].abs() / session_range,
        0.0
    )

    # Did the gap get filled?
    # Gap up filled = session low went below prev_close
    # Gap down filled = session high went above prev_close
    mp["gap_filled"] = np.where(
        mp["gap_pts"] > 0,
        mp["session_low"] <= mp["prev_close"],
        mp["session_high"] >= mp["prev_close"]
    )

    # Open outside previous day's value area?
    mp["open_above_pvah"] = mp["open_price"] > mp["prev_vah"]
    mp["open_below_pval"] = mp["open_price"] < mp["prev_val"]
    mp["open_outside_pva"] = mp["open_above_pvah"] | mp["open_below_pval"]

    # Open above/below previous POC
    mp["open_above_ppoc"] = mp["open_price"] > mp["prev_poc"]

    # Overnight return vs intraday return
    mp["overnight_return"] = mp["gap_pts"]
    mp["intraday_return"] = mp["close_price"] - mp["open_price"]
    mp["total_return"] = mp["close_price"] - mp["prev_close"]

    # What % of total move happened overnight?
    mp["overnight_pct_of_total"] = np.where(
        mp["total_return"].abs() > 0,
        mp["overnight_return"] / mp["total_return"] * 100.0,
        0.0
    )

    return mp


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — VALUE AREA MIGRATION (EXPANSION DETECTION)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_va_migration(mp: pd.DataFrame) -> pd.DataFrame:
    """
    Track how Value Area shifts day-to-day to detect expansion phases.

    VA Migration types:
      - HIGHER: today's VAL > prev VAL AND today's VAH > prev VAH
      - LOWER:  today's VAL < prev VAL AND today's VAH < prev VAH
      - WIDER:  today's VAL < prev VAL AND today's VAH > prev VAH (expansion)
      - NARROWER: today's VAL > prev VAL AND today's VAH < prev VAH (contraction)
      - OVERLAP: partial overlap, not clearly migrating
    """
    mp = mp.copy()

    # Value area overlap with previous day
    mp["va_overlap"] = np.maximum(0,
        np.minimum(mp["vah"], mp["prev_vah"]) - np.maximum(mp["val"], mp["prev_val"])
    )
    prev_var = mp["prev_vah"] - mp["prev_val"]
    mp["va_overlap_pct"] = np.where(prev_var > 0, mp["va_overlap"] / prev_var * 100.0, 100.0)

    # POC migration
    mp["poc_shift"] = mp["poc"] - mp["prev_poc"]
    mp["poc_shift_pct"] = mp["poc_shift"] / mp["prev_poc"] * 100.0

    # VA migration direction
    def _va_migration(row):
        vah, val = row["vah"], row["val"]
        pvah, pval = row["prev_vah"], row["prev_val"]
        if pd.isna(pvah) or pd.isna(pval):
            return "UNKNOWN"
        if val > pval and vah > pvah:
            return "HIGHER"
        if val < pval and vah < pvah:
            return "LOWER"
        if val < pval and vah > pvah:
            return "WIDER"
        if val > pval and vah < pvah:
            return "NARROWER"
        return "OVERLAP"

    mp["va_migration"] = mp.apply(_va_migration, axis=1)

    # Consecutive migration streak
    streak = []
    curr_dir = None
    curr_count = 0
    for _, r in mp.iterrows():
        mig = r["va_migration"]
        if mig in ("HIGHER", "LOWER"):
            if mig == curr_dir:
                curr_count += 1
            else:
                curr_dir = mig
                curr_count = 1
        else:
            curr_dir = None
            curr_count = 0
        streak.append(curr_count)
    mp["va_streak"] = streak

    # Rolling 5-day VA migration score (-5 to +5)
    # +1 for HIGHER, -1 for LOWER, 0 for others
    mp["va_mig_score"] = mp["va_migration"].map(
        {"HIGHER": 1, "LOWER": -1, "WIDER": 0, "NARROWER": 0, "OVERLAP": 0, "UNKNOWN": 0}
    )
    mp["va_mig_5d"] = mp["va_mig_score"].rolling(5, min_periods=1).sum()

    return mp


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — OPENING TYPE DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def classify_opening_type(mp: pd.DataFrame) -> pd.DataFrame:
    """
    Classify opening types per Market Profile theory:

    OPEN-DRIVE (OD):
      - Open outside previous VA
      - First 15-30 min extends aggressively away from prev VA
      - IB broken in open direction only
      - Strongest trend signal

    OPEN-TEST-DRIVE (OTD):
      - Open near previous VA edge
      - Brief test into VA, then reversal out
      - IB eventually broken in test-reversal direction

    OPEN-REJECTION-REVERSE (ORR):
      - Open outside previous VA
      - Fails to extend, reverses back into VA
      - Failed Auction pattern

    OPEN-AUCTION (OA):
      - Open inside previous VA
      - Rotational behavior, no clear direction
      - Classic balance day

    Uses: open price relative to prev VA, IB extensions, close position
    """
    mp = mp.copy()
    types = []
    for _, r in mp.iterrows():
        if pd.isna(r.get("prev_vah")) or pd.isna(r.get("prev_val")):
            types.append("UNKNOWN")
            continue

        o = r["open_price"]
        c = r["close_price"]
        pvah = r["prev_vah"]
        pval = r["prev_val"]
        ib_up = r["ib_broken_up"]
        ib_dn = r["ib_broken_dn"]
        fa_up = r["fa_up"]
        fa_dn = r["fa_dn"]
        close_pos = r["close_pct"]

        open_above_va = o > pvah
        open_below_va = o < pval
        open_inside_va = pval <= o <= pvah

        # OPEN-DRIVE: open outside VA, extends further, strong close
        if open_above_va and ib_up and not ib_dn and close_pos >= 0.65:
            types.append("OD_UP")
        elif open_below_va and ib_dn and not ib_up and close_pos <= 0.35:
            types.append("OD_DN")

        # OPEN-REJECTION-REVERSE: open outside VA, fails, reverses
        elif open_above_va and (fa_up or close_pos <= 0.40):
            types.append("ORR_DN")
        elif open_below_va and (fa_dn or close_pos >= 0.60):
            types.append("ORR_UP")

        # OPEN-TEST-DRIVE: open near VA edge, tests, then drives away
        elif open_inside_va and ib_up and not ib_dn and close_pos >= 0.70:
            types.append("OTD_UP")
        elif open_inside_va and ib_dn and not ib_up and close_pos <= 0.30:
            types.append("OTD_DN")

        # OPEN-AUCTION: inside VA, rotational
        elif open_inside_va:
            types.append("OA")

        else:
            types.append("OTHER")

    mp["opening_type"] = types

    # Is it an "expansion opening"? (OD or OTD)
    mp["expansion_opening"] = mp["opening_type"].isin(
        ["OD_UP", "OD_DN", "OTD_UP", "OTD_DN"]
    )

    return mp


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — MULTI-TIMEFRAME EXPANSION SCORING
# ═══════════════════════════════════════════════════════════════════════════════

def compute_expansion_score(mp: pd.DataFrame) -> pd.DataFrame:
    """
    Score each day's expansion potential on 0-20 scale.
    Higher score = stronger expansion signal.

    Components:
      1. VA Migration 5d score (0-4):  abs(va_mig_5d) >= 3 → 4, >=2 → 2, else 0
      2. Day type (0-4):               TREND → 4, NORMAL_VAR → 2, NORMAL → 0
      3. Opening type (0-4):           OD → 4, OTD → 3, ORR → 1, OA → 0
      4. Close position (0-2):         top/bottom 25% → 2, top/bottom 50% → 1
      5. Gap strength (0-2):           |gap| > 0.5% → 2, > 0.2% → 1
      6. IB break direction (0-2):     one-sided break → 2, both → 1, none → 0
      7. Range ratio (0-2):            > 2.5 → 2, > 1.5 → 1, else 0
    """
    mp = mp.copy()

    # Component 1: VA Migration momentum
    mp["sc_va_mig"] = np.where(mp["va_mig_5d"].abs() >= 3, 4,
                      np.where(mp["va_mig_5d"].abs() >= 2, 2, 0))

    # Component 2: Day type
    dt_map = {
        "TREND_UP": 4, "TREND_DN": 4,
        "NORMAL_VAR_UP": 2, "NORMAL_VAR_DN": 2,
        "DOUBLE_DIST": 1, "FAILED_AUCTION": 1,
        "NORMAL": 0, "UNKNOWN": 0
    }
    mp["sc_day_type"] = mp["day_type"].map(dt_map).fillna(0).astype(int)

    # Component 3: Opening type
    ot_map = {
        "OD_UP": 4, "OD_DN": 4,
        "OTD_UP": 3, "OTD_DN": 3,
        "ORR_UP": 1, "ORR_DN": 1,
        "OA": 0, "OTHER": 0, "UNKNOWN": 0
    }
    mp["sc_opening"] = mp["opening_type"].map(ot_map).fillna(0).astype(int)

    # Component 4: Close position (extreme = expansion confirmed)
    mp["sc_close_pos"] = np.where(
        (mp["close_pct"] >= 0.75) | (mp["close_pct"] <= 0.25), 2,
        np.where((mp["close_pct"] >= 0.60) | (mp["close_pct"] <= 0.40), 1, 0)
    )

    # Component 5: Gap strength
    mp["sc_gap"] = np.where(mp["gap_abs_pct"] >= 0.5, 2,
                   np.where(mp["gap_abs_pct"] >= 0.2, 1, 0))

    # Component 6: IB break directionality
    mp["sc_ib_break"] = np.where(
        mp["ib_broken_up"] != mp["ib_broken_dn"], 2,  # one-sided
        np.where(mp["ib_broken_up"] & mp["ib_broken_dn"], 1, 0)  # both or none
    )

    # Component 7: Range ratio
    mp["sc_range"] = np.where(mp["range_ratio"] >= 2.5, 2,
                    np.where(mp["range_ratio"] >= 1.5, 1, 0))

    # Total expansion score
    mp["expansion_score"] = (
        mp["sc_va_mig"] + mp["sc_day_type"] + mp["sc_opening"] +
        mp["sc_close_pos"] + mp["sc_gap"] + mp["sc_ib_break"] + mp["sc_range"]
    )

    # Direction of expansion (based on all directional signals)
    dir_score = (
        mp["va_mig_5d"] +
        np.where(mp["gap_pts"] > 0, 1, -1) +
        np.where(mp["close_pct"] > 0.5, 1, -1) +
        np.where(mp["day_type"].str.contains("UP"), 2, 0) +
        np.where(mp["day_type"].str.contains("DN"), -2, 0)
    )
    mp["expansion_direction"] = np.where(dir_score > 0, "BULLISH",
                                np.where(dir_score < 0, "BEARISH", "NEUTRAL"))

    return mp


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 7 — CARRY SIGNAL GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def generate_carry_signals(mp: pd.DataFrame) -> pd.DataFrame:
    """
    Determine at end-of-day whether to CARRY a position overnight.

    CARRY LONG (buy CE before close, hold overnight):
      - expansion_score >= 10
      - expansion_direction == BULLISH
      - close in top 30% of day's range
      - IB broken up but NOT down
      - VA migrating HIGHER (streak >= 2)

    CARRY SHORT (buy PE before close, hold overnight):
      - expansion_score >= 10
      - expansion_direction == BEARISH
      - close in bottom 30% of day's range
      - IB broken down but NOT up
      - VA migrating LOWER (streak >= 2)

    NO CARRY:
      - Score < 10 OR direction NEUTRAL OR close in middle 40%

    Also outputs CARRY STRENGTH (how many criteria passed out of 5).
    """
    mp = mp.copy()

    carry_signals = []
    for _, r in mp.iterrows():
        score = r["expansion_score"]
        direction = r["expansion_direction"]
        close_pos = r["close_pct"]
        ib_up = r["ib_broken_up"]
        ib_dn = r["ib_broken_dn"]
        va_streak = r["va_streak"]
        va_mig = r["va_migration"]

        # ── CARRY LONG checks ────────────────────────────────────────────
        long_checks = [
            score >= 10,
            direction == "BULLISH",
            close_pos >= 0.70,
            ib_up and not ib_dn,
            va_streak >= 2 and va_mig == "HIGHER",
        ]
        long_strength = sum(long_checks)

        # ── CARRY SHORT checks ───────────────────────────────────────────
        short_checks = [
            score >= 10,
            direction == "BEARISH",
            close_pos <= 0.30,
            ib_dn and not ib_up,
            va_streak >= 2 and va_mig == "LOWER",
        ]
        short_strength = sum(short_checks)

        if long_strength >= 3:
            carry_signals.append(("CARRY_LONG", long_strength))
        elif short_strength >= 3:
            carry_signals.append(("CARRY_SHORT", short_strength))
        else:
            carry_signals.append(("NO_CARRY", 0))

    mp["carry_signal"] = [s[0] for s in carry_signals]
    mp["carry_strength"] = [s[1] for s in carry_signals]

    return mp


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 8 — BACKTEST OVERNIGHT CARRY
# ═══════════════════════════════════════════════════════════════════════════════

def backtest_carry(mp: pd.DataFrame) -> pd.DataFrame:
    """
    Simulate overnight carry trades:

    Entry: Close of carry signal day (use prev close as proxy)
    Exit:  Next day's IB completion (10:15) or next day's close

    Measure:
      - Overnight gap return (open - prev close)
      - IB exit return (option at 10:15 vs prev close entry)
      - EOD exit return (next day close vs prev close entry)
      - Did the gap help or hurt?

    For options:
      - CE carry long → CE premium should rise if spot gaps up
      - PE carry short → PE premium should rise if spot gaps down
      - Approximate option return as leverage × spot return (use 3.5× for ATM weekly)

    Since we don't track exact option prices at close/open here, we compute
    the SPOT-LEVEL returns and convert to estimated option returns.
    """
    mp = mp.copy()

    # Next-day data
    mp["next_open"] = mp["open_price"].shift(-1)
    mp["next_close"] = mp["close_price"].shift(-1)
    mp["next_ibh"] = mp["ibh"].shift(-1)
    mp["next_ibl"] = mp["ibl"].shift(-1)
    mp["next_high"] = mp["session_high"].shift(-1)
    mp["next_low"] = mp["session_low"].shift(-1)
    mp["next_day_type"] = mp["day_type"].shift(-1)
    mp["next_date"] = mp["date"].shift(-1)

    OPT_LEVERAGE = 3.5  # ATM weekly option leverage vs spot

    results = []
    for i, r in mp.iterrows():
        if r["carry_signal"] == "NO_CARRY":
            continue
        if pd.isna(r["next_open"]) or pd.isna(r["next_close"]):
            continue

        entry_spot = r["close_price"]
        is_long = r["carry_signal"] == "CARRY_LONG"

        # Overnight gap
        gap_pts = r["next_open"] - entry_spot
        gap_pct = gap_pts / entry_spot * 100.0
        gap_favorable = (is_long and gap_pts > 0) or (not is_long and gap_pts < 0)

        # Next day close return
        eod_pts = r["next_close"] - entry_spot
        eod_pct = eod_pts / entry_spot * 100.0
        eod_favorable = (is_long and eod_pts > 0) or (not is_long and eod_pts < 0)

        # IB exit approximation (midpoint of next day's IB)
        ib_mid = (r["next_ibh"] + r["next_ibl"]) / 2.0
        ib_pts = ib_mid - entry_spot
        ib_pct = ib_pts / entry_spot * 100.0

        # Max favorable excursion next day
        if is_long:
            max_fav = (r["next_high"] - entry_spot) / entry_spot * 100.0
            max_adv = (entry_spot - r["next_low"]) / entry_spot * 100.0
        else:
            max_fav = (entry_spot - r["next_low"]) / entry_spot * 100.0
            max_adv = (r["next_high"] - entry_spot) / entry_spot * 100.0

        # Estimated option returns (leverage × spot return)
        opt_gap_return = abs(gap_pct) * OPT_LEVERAGE * (1 if gap_favorable else -1)
        opt_eod_return = abs(eod_pct) * OPT_LEVERAGE * (1 if eod_favorable else -1)
        opt_max_fav = max_fav * OPT_LEVERAGE
        opt_max_adv = max_adv * OPT_LEVERAGE

        results.append({
            "date": r["date"],
            "carry_signal": r["carry_signal"],
            "carry_strength": r["carry_strength"],
            "expansion_score": r["expansion_score"],
            "day_type": r["day_type"],
            "opening_type": r["opening_type"],
            "next_day_type": r["next_day_type"],
            "next_date": r["next_date"],
            "entry_spot": round(entry_spot, 2),
            "gap_pts": round(gap_pts, 2),
            "gap_pct": round(gap_pct, 4),
            "gap_favorable": gap_favorable,
            "eod_pct": round(eod_pct, 4),
            "eod_favorable": eod_favorable,
            "ib_pct": round(ib_pct, 4),
            "max_fav_spot_pct": round(max_fav, 4),
            "max_adv_spot_pct": round(max_adv, 4),
            "opt_gap_return": round(opt_gap_return, 2),
            "opt_eod_return": round(opt_eod_return, 2),
            "opt_max_fav": round(opt_max_fav, 2),
            "opt_max_adv": round(opt_max_adv, 2),
        })

    return pd.DataFrame(results)


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 9 — ANALYSIS & REPORTING
# ═══════════════════════════════════════════════════════════════════════════════

def print_day_type_analysis(mp: pd.DataFrame):
    print("\n" + "=" * 72)
    print("  DAY-TYPE CLASSIFICATION RESULTS")
    print("=" * 72)

    counts = mp["day_type"].value_counts()
    total = len(mp)
    print(f"\n  Total trading days: {total}")
    print(f"\n  {'Day Type':<20} {'Count':>6} {'Pct':>8} {'Avg Range Ratio':>16} {'Avg Close Pos':>14}")
    print("  " + "-" * 68)
    for dt in ["TREND_UP", "TREND_DN", "NORMAL_VAR_UP", "NORMAL_VAR_DN",
               "DOUBLE_DIST", "FAILED_AUCTION", "NORMAL", "UNKNOWN"]:
        subset = mp[mp["day_type"] == dt]
        if len(subset) == 0: continue
        avg_rr = subset["range_ratio"].mean()
        avg_cp = subset["close_pct"].mean()
        print(f"  {dt:<20} {len(subset):>6} {len(subset)/total*100:>7.1f}% {avg_rr:>14.2f}× {avg_cp:>14.2f}")

    # Day type transitions
    print(f"\n  Day-type transition analysis (what follows each type):")
    mp_shifted = mp.copy()
    mp_shifted["next_day_type"] = mp_shifted["day_type"].shift(-1)
    for dt in ["TREND_UP", "TREND_DN", "NORMAL_VAR_UP", "NORMAL_VAR_DN", "NORMAL"]:
        subset = mp_shifted[mp_shifted["day_type"] == dt].dropna(subset=["next_day_type"])
        if len(subset) < 3: continue
        next_counts = subset["next_day_type"].value_counts()
        top3 = next_counts.head(3)
        top3_str = ", ".join(f"{k}={v}" for k, v in top3.items())
        print(f"    After {dt:<20} → {top3_str}")


def print_overnight_gap_analysis(mp: pd.DataFrame):
    print("\n" + "=" * 72)
    print("  OVERNIGHT GAP ANALYSIS")
    print("=" * 72)

    valid = mp.dropna(subset=["gap_pct"])

    print(f"\n  Total days with gap data: {len(valid)}")
    print(f"\n  Gap Statistics:")
    print(f"    Mean gap:   {valid['gap_pct'].mean():+.3f}%  ({valid['gap_pts'].mean():+.0f} pts)")
    print(f"    Median gap: {valid['gap_pct'].median():+.3f}%  ({valid['gap_pts'].median():+.0f} pts)")
    print(f"    Std gap:    {valid['gap_pct'].std():.3f}%")
    print(f"    Max gap up: {valid['gap_pct'].max():+.3f}%  ({valid['gap_pts'].max():+.0f} pts)")
    print(f"    Max gap dn: {valid['gap_pct'].min():+.3f}%  ({valid['gap_pts'].min():+.0f} pts)")
    print(f"    Gap up days:   {(valid['gap_pts'] > 0).sum()} ({(valid['gap_pts'] > 0).mean()*100:.1f}%)")
    print(f"    Gap dn days:   {(valid['gap_pts'] < 0).sum()} ({(valid['gap_pts'] < 0).mean()*100:.1f}%)")

    # Gap size buckets
    print(f"\n  Gap Size Distribution:")
    print(f"    {'Bucket':<20} {'Count':>6} {'Pct':>7} {'Avg Gap Fill':>13} {'Avg Day Return':>15}")
    print("    " + "-" * 64)
    bins = [
        ("Tiny  (< 0.1%)", valid["gap_abs_pct"] < 0.1),
        ("Small (0.1-0.3%)", (valid["gap_abs_pct"] >= 0.1) & (valid["gap_abs_pct"] < 0.3)),
        ("Medium(0.3-0.5%)", (valid["gap_abs_pct"] >= 0.3) & (valid["gap_abs_pct"] < 0.5)),
        ("Large (0.5-1.0%)", (valid["gap_abs_pct"] >= 0.5) & (valid["gap_abs_pct"] < 1.0)),
        ("Huge  (>= 1.0%)",  valid["gap_abs_pct"] >= 1.0),
    ]
    for name, mask in bins:
        sub = valid[mask]
        if len(sub) == 0: continue
        fill_rate = sub["gap_filled"].mean() * 100
        avg_ret = sub["total_return"].mean()
        print(f"    {name:<20} {len(sub):>6} {len(sub)/len(valid)*100:>6.1f}% {fill_rate:>11.1f}% {avg_ret:>+14.0f}")

    # Overnight contribution to total move
    print(f"\n  Overnight Move Contribution:")
    aligned = valid[valid["total_return"].abs() > 10]  # filter noise
    if len(aligned) > 0:
        same_dir = aligned[aligned["overnight_return"] * aligned["total_return"] > 0]
        print(f"    Days where overnight gap is in SAME direction as daily close: "
              f"{len(same_dir)}/{len(aligned)} ({len(same_dir)/len(aligned)*100:.1f}%)")
        if len(same_dir) > 0:
            med_contrib = same_dir["overnight_pct_of_total"].median()
            print(f"    Median overnight contribution (when same direction): {med_contrib:.1f}%")

    # Gap continuation by day type
    print(f"\n  Gap Direction vs Day Close Direction:")
    valid_gaps = valid[valid["gap_abs_pct"] >= 0.1]  # meaningful gaps
    if len(valid_gaps) > 0:
        gap_continues = valid_gaps[
            (valid_gaps["gap_pts"] > 0) & (valid_gaps["close_price"] > valid_gaps["open_price"]) |
            (valid_gaps["gap_pts"] < 0) & (valid_gaps["close_price"] < valid_gaps["open_price"])
        ]
        print(f"    Gap continuation rate (gap >= 0.1%): {len(gap_continues)}/{len(valid_gaps)} "
              f"({len(gap_continues)/len(valid_gaps)*100:.1f}%)")

    # Open outside prev VA
    outside = valid[valid["open_outside_pva"]]
    if len(outside) > 0:
        print(f"\n  Open OUTSIDE Previous Value Area: {len(outside)}/{len(valid)} "
              f"({len(outside)/len(valid)*100:.1f}%)")
        out_trend = outside[outside["day_type"].str.contains("TREND")]
        print(f"    → Became trend day: {len(out_trend)}/{len(outside)} "
              f"({len(out_trend)/len(outside)*100:.1f}%)")
        out_gap_fill = outside[outside["gap_filled"]]
        print(f"    → Gap filled: {len(out_gap_fill)}/{len(outside)} "
              f"({len(out_gap_fill)/len(outside)*100:.1f}%)")


def print_va_migration_analysis(mp: pd.DataFrame):
    print("\n" + "=" * 72)
    print("  VALUE AREA MIGRATION — EXPANSION DETECTION")
    print("=" * 72)

    valid = mp.dropna(subset=["prev_vah"])

    counts = valid["va_migration"].value_counts()
    print(f"\n  VA Migration Types:")
    for mig, cnt in counts.items():
        subset = valid[valid["va_migration"] == mig]
        avg_range = subset["range_ratio"].mean()
        print(f"    {mig:<12} {cnt:>5} ({cnt/len(valid)*100:>5.1f}%)  avg range ratio: {avg_range:.2f}×")

    # Streaks
    streaks = valid[valid["va_streak"] >= 2]
    print(f"\n  Multi-day VA Migration Streaks (consecutive HIGHER/LOWER):")
    print(f"    Days with streak >= 2: {len(streaks)} ({len(streaks)/len(valid)*100:.1f}%)")
    if len(streaks) > 0:
        print(f"    Max streak: {valid['va_streak'].max()}")
        avg_ret = streaks.groupby("va_migration")["total_return"].mean()
        for mig, ret in avg_ret.items():
            print(f"    Avg total return during {mig} streak: {ret:+.0f} pts")

    # 5-day momentum
    print(f"\n  5-day VA Migration Momentum Score:")
    strong_bull = valid[valid["va_mig_5d"] >= 3]
    strong_bear = valid[valid["va_mig_5d"] <= -3]
    neutral = valid[(valid["va_mig_5d"] > -3) & (valid["va_mig_5d"] < 3)]
    print(f"    Strong bullish (score >= +3):  {len(strong_bull):>4} days, "
          f"avg next return: {strong_bull['total_return'].mean():+.0f} pts" if len(strong_bull) > 0 else
          f"    Strong bullish (score >= +3): 0 days")
    print(f"    Strong bearish (score <= -3):  {len(strong_bear):>4} days, "
          f"avg next return: {strong_bear['total_return'].mean():+.0f} pts" if len(strong_bear) > 0 else
          f"    Strong bearish (score <= -3): 0 days")
    print(f"    Neutral (-3 < score < +3):     {len(neutral):>4} days")

    # VA overlap analysis
    print(f"\n  VA Overlap with Previous Day:")
    print(f"    {'Overlap Band':<25} {'Count':>6} {'Day Type (most common)':>25} {'Avg Return':>12}")
    print("    " + "-" * 72)
    overlap_bins = [
        ("No overlap (0%)",     (valid["va_overlap_pct"] <= 0)),
        ("Low (1-30%)",         (valid["va_overlap_pct"] > 0) & (valid["va_overlap_pct"] <= 30)),
        ("Medium (30-60%)",     (valid["va_overlap_pct"] > 30) & (valid["va_overlap_pct"] <= 60)),
        ("High (60-90%)",       (valid["va_overlap_pct"] > 60) & (valid["va_overlap_pct"] <= 90)),
        ("Full overlap (>90%)", (valid["va_overlap_pct"] > 90)),
    ]
    for name, mask in overlap_bins:
        sub = valid[mask]
        if len(sub) == 0: continue
        top_dt = sub["day_type"].mode().iloc[0] if len(sub) > 0 else "N/A"
        avg_ret = sub["total_return"].mean()
        print(f"    {name:<25} {len(sub):>6} {top_dt:>25} {avg_ret:>+11.0f}")


def print_expansion_score_analysis(mp: pd.DataFrame):
    print("\n" + "=" * 72)
    print("  MULTI-TIMEFRAME EXPANSION SCORING")
    print("=" * 72)

    print(f"\n  Expansion Score Distribution:")
    print(f"    {'Score Band':<20} {'Count':>6} {'Pct':>7} {'Avg Range':>10} {'Avg |Return|':>13} "
          f"{'Trend Days':>11}")
    print("    " + "-" * 72)
    bands = [
        ("Low (0-5)",    (mp["expansion_score"] >= 0) & (mp["expansion_score"] <= 5)),
        ("Medium (6-9)", (mp["expansion_score"] >= 6) & (mp["expansion_score"] <= 9)),
        ("High (10-13)", (mp["expansion_score"] >= 10) & (mp["expansion_score"] <= 13)),
        ("Very High (14+)", (mp["expansion_score"] >= 14)),
    ]
    for name, mask in bands:
        sub = mp[mask]
        if len(sub) == 0: continue
        avg_rr = sub["range_ratio"].mean()
        avg_ret = sub["total_return"].abs().mean()
        trend = sub["day_type"].str.contains("TREND").sum()
        print(f"    {name:<20} {len(sub):>6} {len(sub)/len(mp)*100:>6.1f}% {avg_rr:>8.2f}× "
              f"{avg_ret:>+12.0f} {trend:>5}/{len(sub)}")

    # Carry signal predictive power
    print(f"\n  Direction Score by Expansion Band:")
    for name, mask in bands:
        sub = mp[mask]
        if len(sub) == 0: continue
        bull = sub[sub["expansion_direction"] == "BULLISH"]
        bear = sub[sub["expansion_direction"] == "BEARISH"]
        neut = sub[sub["expansion_direction"] == "NEUTRAL"]
        print(f"    {name}: BULL={len(bull)} BEAR={len(bear)} NEUT={len(neut)}")

    # Opening type by expansion score
    print(f"\n  Opening Type vs Expansion Score:")
    for ot in ["OD_UP", "OD_DN", "OTD_UP", "OTD_DN", "ORR_UP", "ORR_DN", "OA"]:
        sub = mp[mp["opening_type"] == ot]
        if len(sub) == 0: continue
        avg_score = sub["expansion_score"].mean()
        avg_ret = sub["total_return"].mean()
        print(f"    {ot:<10} n={len(sub):>3}  avg_score={avg_score:.1f}  avg_return={avg_ret:+.0f} pts")


def print_carry_backtest(carry_df: pd.DataFrame):
    print("\n" + "=" * 72)
    print("  OVERNIGHT CARRY BACKTEST RESULTS")
    print("=" * 72)

    if len(carry_df) == 0:
        print("\n  No carry signals generated. Adjust thresholds.")
        return

    print(f"\n  Total carry signals: {len(carry_df)}")
    for sig in ["CARRY_LONG", "CARRY_SHORT"]:
        sub = carry_df[carry_df["carry_signal"] == sig]
        if len(sub) == 0:
            print(f"\n  {sig}: 0 signals")
            continue

        print(f"\n  {sig}: {len(sub)} signals")
        print(f"    Gap favorable rate: {sub['gap_favorable'].mean()*100:.1f}%")
        print(f"    Avg gap (spot):     {sub['gap_pct'].mean():+.3f}%")
        print(f"    EOD favorable rate: {sub['eod_favorable'].mean()*100:.1f}%")
        print(f"    Avg EOD (spot):     {sub['eod_pct'].mean():+.3f}%")
        print(f"    Avg max favorable:  {sub['max_fav_spot_pct'].mean():+.3f}% spot / "
              f"{sub['opt_max_fav'].mean():+.1f}% option")
        print(f"    Avg max adverse:    {sub['max_adv_spot_pct'].mean():.3f}% spot / "
              f"{sub['opt_max_adv'].mean():.1f}% option")

        # By carry strength
        for strength in sorted(sub["carry_strength"].unique()):
            ss = sub[sub["carry_strength"] == strength]
            print(f"\n    Strength {strength}/5 ({len(ss)} trades):")
            print(f"      Gap fav: {ss['gap_favorable'].mean()*100:.1f}%  "
                  f"EOD fav: {ss['eod_favorable'].mean()*100:.1f}%  "
                  f"Avg opt EOD: {ss['opt_eod_return'].mean():+.1f}%  "
                  f"Avg opt max: {ss['opt_max_fav'].mean():+.1f}%")

    # Combined P&L simulation
    print(f"\n  Combined Carry Strategy P&L (₹1L start, 10% alloc per trade):")
    alloc = 0.10
    equity = 100_000.0
    equity_curve = [equity]
    for _, r in carry_df.iterrows():
        ret = r["opt_eod_return"] / 100.0
        equity = equity + equity * alloc * max(ret, -0.50)
        equity_curve.append(equity)

    print(f"    Final equity:    ₹{equity:,.0f}")
    print(f"    Total return:    {(equity - 100000) / 100000 * 100:+.1f}%")
    print(f"    Trades taken:    {len(carry_df)}")
    peak = max(equity_curve)
    trough = min(equity_curve[equity_curve.index(peak):]) if peak == max(equity_curve) else equity
    print(f"    Max drawdown:    {(trough - peak) / peak * 100:.1f}%")

    # Equity using gap exit only (overnight profit-taking)
    equity_gap = 100_000.0
    for _, r in carry_df.iterrows():
        ret = r["opt_gap_return"] / 100.0
        equity_gap = equity_gap + equity_gap * alloc * max(ret, -0.50)
    print(f"\n    Gap-exit-only equity: ₹{equity_gap:,.0f} ({(equity_gap-100000)/100000*100:+.1f}%)")


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 10 — VISUALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def plot_dashboard(mp: pd.DataFrame, carry_df: pd.DataFrame):
    fig = plt.figure(figsize=(24, 20))
    fig.suptitle("SENSEX Expansion & Overnight Carry Analysis", fontsize=16, fontweight="bold")
    gs = gridspec.GridSpec(4, 3, hspace=0.35, wspace=0.30)

    dates = pd.to_datetime(mp["date"])

    # 1. Day type distribution
    ax1 = fig.add_subplot(gs[0, 0])
    dt_counts = mp["day_type"].value_counts()
    colors = {
        "TREND_UP": "#2ecc71", "TREND_DN": "#e74c3c",
        "NORMAL_VAR_UP": "#27ae60", "NORMAL_VAR_DN": "#c0392b",
        "DOUBLE_DIST": "#f39c12", "FAILED_AUCTION": "#9b59b6",
        "NORMAL": "#95a5a6", "UNKNOWN": "#bdc3c7"
    }
    ax1.barh([k for k in dt_counts.index], [v for v in dt_counts.values],
             color=[colors.get(k, "#999") for k in dt_counts.index])
    ax1.set_title("Day Type Distribution", fontweight="bold")
    ax1.set_xlabel("Count")

    # 2. Expansion score over time
    ax2 = fig.add_subplot(gs[0, 1:])
    ax2.bar(dates, mp["expansion_score"],
            color=np.where(mp["expansion_direction"] == "BULLISH", "#2ecc71",
                  np.where(mp["expansion_direction"] == "BEARISH", "#e74c3c", "#95a5a6")),
            alpha=0.7, width=1)
    ax2.axhline(10, color="orange", ls="--", alpha=0.8, label="Carry threshold (10)")
    ax2.axhline(14, color="red", ls="--", alpha=0.8, label="Strong expansion (14)")
    ax2.set_title("Expansion Score Over Time", fontweight="bold")
    ax2.set_ylabel("Score (0-20)")
    ax2.legend(fontsize=8)

    # 3. Overnight gap distribution
    ax3 = fig.add_subplot(gs[1, 0])
    valid_gaps = mp.dropna(subset=["gap_pct"])
    ax3.hist(valid_gaps["gap_pct"], bins=50, color="#3498db", alpha=0.7, edgecolor="white")
    ax3.axvline(0, color="black", ls="-", alpha=0.5)
    ax3.set_title("Overnight Gap Distribution (%)", fontweight="bold")
    ax3.set_xlabel("Gap %")

    # 4. VA migration streak
    ax4 = fig.add_subplot(gs[1, 1])
    valid_va = mp.dropna(subset=["va_mig_5d"])
    ax4.bar(pd.to_datetime(valid_va["date"]), valid_va["va_mig_5d"],
            color=np.where(valid_va["va_mig_5d"] > 0, "#2ecc71", "#e74c3c"),
            alpha=0.7, width=1)
    ax4.axhline(3, color="green", ls="--", alpha=0.5, label="Bullish +3")
    ax4.axhline(-3, color="red", ls="--", alpha=0.5, label="Bearish -3")
    ax4.set_title("5-day VA Migration Momentum", fontweight="bold")
    ax4.set_ylabel("Score")
    ax4.legend(fontsize=8)

    # 5. Opening type distribution
    ax5 = fig.add_subplot(gs[1, 2])
    ot_counts = mp["opening_type"].value_counts()
    ot_colors = {
        "OD_UP": "#2ecc71", "OD_DN": "#e74c3c",
        "OTD_UP": "#27ae60", "OTD_DN": "#c0392b",
        "ORR_UP": "#3498db", "ORR_DN": "#e67e22",
        "OA": "#95a5a6", "OTHER": "#bdc3c7", "UNKNOWN": "#ddd"
    }
    ax5.barh([k for k in ot_counts.index], [v for v in ot_counts.values],
             color=[ot_colors.get(k, "#999") for k in ot_counts.index])
    ax5.set_title("Opening Type Distribution", fontweight="bold")

    # 6. Spot price with carry signals overlaid
    ax6 = fig.add_subplot(gs[2, :])
    ax6.plot(dates, mp["close_price"], color="#2c3e50", linewidth=1, alpha=0.8, label="SENSEX Close")
    # Mark carry long
    long_mask = mp["carry_signal"] == "CARRY_LONG"
    short_mask = mp["carry_signal"] == "CARRY_SHORT"
    ax6.scatter(dates[long_mask], mp.loc[long_mask, "close_price"],
                marker="^", color="#2ecc71", s=80, zorder=5, label="Carry Long")
    ax6.scatter(dates[short_mask], mp.loc[short_mask, "close_price"],
                marker="v", color="#e74c3c", s=80, zorder=5, label="Carry Short")
    ax6.set_title("SENSEX with Carry Signals", fontweight="bold")
    ax6.set_ylabel("Price")
    ax6.legend(fontsize=8)

    # 7. Carry trade results (if we have them)
    if len(carry_df) > 0:
        ax7 = fig.add_subplot(gs[3, 0])
        ax7.bar(range(len(carry_df)), carry_df["opt_eod_return"],
                color=np.where(carry_df["opt_eod_return"] > 0, "#2ecc71", "#e74c3c"),
                alpha=0.7)
        ax7.axhline(0, color="black", ls="-", alpha=0.5)
        ax7.set_title("Carry Trade Option Returns (%)", fontweight="bold")
        ax7.set_xlabel("Trade #")
        ax7.set_ylabel("Est. Option Return %")

        # 8. Equity curve
        ax8 = fig.add_subplot(gs[3, 1])
        alloc = 0.10
        eq = 100_000.0
        eq_curve = [eq]
        for _, r in carry_df.iterrows():
            ret = r["opt_eod_return"] / 100.0
            eq = eq + eq * alloc * max(ret, -0.50)
            eq_curve.append(eq)
        ax8.plot(eq_curve, color="#2c3e50", linewidth=2)
        ax8.axhline(100_000, color="gray", ls="--", alpha=0.5)
        ax8.set_title("Carry Strategy Equity Curve (₹1L, 10% alloc)", fontweight="bold")
        ax8.set_ylabel("Equity (₹)")
        ax8.set_xlabel("Trade #")

        # 9. Carry strength vs win rate
        ax9 = fig.add_subplot(gs[3, 2])
        strengths = sorted(carry_df["carry_strength"].unique())
        win_rates = []
        counts = []
        for s in strengths:
            sub = carry_df[carry_df["carry_strength"] == s]
            wr = (sub["opt_eod_return"] > 0).mean() * 100
            win_rates.append(wr)
            counts.append(len(sub))
        ax9.bar(strengths, win_rates, color="#3498db", alpha=0.7)
        for s, wr, cnt in zip(strengths, win_rates, counts):
            ax9.text(s, wr + 1, f"n={cnt}", ha="center", fontsize=9)
        ax9.set_title("Win Rate by Carry Strength", fontweight="bold")
        ax9.set_xlabel("Carry Strength (out of 5)")
        ax9.set_ylabel("Win Rate %")
        ax9.set_ylim(0, 100)
    else:
        ax7 = fig.add_subplot(gs[3, :])
        ax7.text(0.5, 0.5, "No carry signals generated", ha="center", va="center",
                 fontsize=14, transform=ax7.transAxes)

    plt.savefig(OUTPUT_ROOT / "expansion_dashboard.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\n  Dashboard saved: {OUTPUT_ROOT / 'expansion_dashboard.png'}")


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 11 — PREDICTIVE ANALYSIS: DOES TODAY PREDICT TOMORROW?
# ═══════════════════════════════════════════════════════════════════════════════

def predictive_analysis(mp: pd.DataFrame):
    """
    Key question: Can today's signals predict TOMORROW's expansion?

    For overnight carry to work, we need today's end-of-day signals
    to predict tomorrow's behavior.
    """
    print("\n" + "=" * 72)
    print("  PREDICTIVE ANALYSIS: TODAY → TOMORROW")
    print("=" * 72)

    mp = mp.copy()
    mp["next_total_return"] = mp["total_return"].shift(-1)
    mp["next_range_ratio"] = mp["range_ratio"].shift(-1)
    mp["next_day_type"] = mp["day_type"].shift(-1)
    mp["next_expansion_score"] = mp["expansion_score"].shift(-1)
    mp["next_gap_pct"] = mp["gap_pct"].shift(-1)

    valid = mp.dropna(subset=["next_total_return"])

    # 1. Does today's expansion score predict tomorrow's range?
    print(f"\n  1. Today's expansion score → Tomorrow's range:")
    for name, mask in [
        ("Low (0-5)",      valid["expansion_score"] <= 5),
        ("Medium (6-9)",   (valid["expansion_score"] >= 6) & (valid["expansion_score"] <= 9)),
        ("High (10-13)",   (valid["expansion_score"] >= 10) & (valid["expansion_score"] <= 13)),
        ("Very High (14+)", valid["expansion_score"] >= 14),
    ]:
        sub = valid[mask]
        if len(sub) < 3: continue
        print(f"    {name:<20} n={len(sub):>3}  → next range_ratio: {sub['next_range_ratio'].mean():.2f}×  "
              f"next |return|: {sub['next_total_return'].abs().mean():.0f} pts")

    # 2. Does today's trend day predict tomorrow's gap direction?
    print(f"\n  2. Today's trend day → Tomorrow's gap direction:")
    for dt in ["TREND_UP", "TREND_DN"]:
        sub = valid[valid["day_type"] == dt]
        if len(sub) < 3: continue
        gap_same_dir = sub[(sub["day_type"] == "TREND_UP") & (sub["next_gap_pct"] > 0) |
                           (sub["day_type"] == "TREND_DN") & (sub["next_gap_pct"] < 0)]
        print(f"    After {dt}: gap continues same direction in "
              f"{len(gap_same_dir)}/{len(sub)} ({len(gap_same_dir)/len(sub)*100:.1f}%) cases")
        print(f"      Avg next gap: {sub['next_gap_pct'].mean():+.3f}%  "
              f"Avg next |return|: {sub['next_total_return'].abs().mean():.0f} pts")

    # 3. VA streak → continuation?
    print(f"\n  3. VA migration streak → Next day continuation:")
    for streak_min in [2, 3, 4]:
        higher_streak = valid[(valid["va_streak"] >= streak_min) & (valid["va_migration"] == "HIGHER")]
        lower_streak = valid[(valid["va_streak"] >= streak_min) & (valid["va_migration"] == "LOWER")]
        if len(higher_streak) >= 3:
            cont = higher_streak[higher_streak["next_total_return"] > 0]
            print(f"    HIGHER streak >= {streak_min}: n={len(higher_streak)}  "
                  f"next day up: {len(cont)/len(higher_streak)*100:.1f}%  "
                  f"avg next return: {higher_streak['next_total_return'].mean():+.0f} pts")
        if len(lower_streak) >= 3:
            cont = lower_streak[lower_streak["next_total_return"] < 0]
            print(f"    LOWER  streak >= {streak_min}: n={len(lower_streak)}  "
                  f"next day dn: {len(cont)/len(lower_streak)*100:.1f}%  "
                  f"avg next return: {lower_streak['next_total_return'].mean():+.0f} pts")

    # 4. Opening type on the day AFTER various signals
    print(f"\n  4. Carry signal → Next day opening type:")
    for sig in ["CARRY_LONG", "CARRY_SHORT"]:
        sub = valid[valid["carry_signal"] == sig]
        if len(sub) < 2: continue
        next_ot = sub["next_day_type"].value_counts().head(3)
        print(f"    After {sig} ({len(sub)} signals): {dict(next_ot)}")

    # 5. Close position → next day gap
    print(f"\n  5. Close position → Next day gap:")
    for name, lo, hi in [("Top 25% close", 0.75, 1.0), ("Bottom 25% close", 0.0, 0.25),
                          ("Middle 50%", 0.25, 0.75)]:
        sub = valid[(valid["close_pct"] >= lo) & (valid["close_pct"] < hi)]
        if len(sub) < 3: continue
        print(f"    {name:<20} n={len(sub):>3}  avg next gap: {sub['next_gap_pct'].mean():+.3f}%  "
              f"gap same dir: {((sub['close_pct'] > 0.5) == (sub['next_gap_pct'] > 0)).mean()*100:.1f}%")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    print("=" * 72)
    print("  SENSEX EXPANSION & OVERNIGHT CARRY ANALYSIS")
    print("  Using Market Profile Data (Apr'25 — Apr'26)")
    print("=" * 72)

    # ── 1. Load data ──────────────────────────────────────────────────────
    print("\n[1] Loading existing MP data …")
    mp = _load_daily_mp()
    opt_mp = _load_option_mp()
    print(f"    Daily spot MP: {len(mp)} days ({mp['date'].min()} → {mp['date'].max()})")
    print(f"    Option MP: {len(opt_mp)} records")

    # ── 2. Classify days ──────────────────────────────────────────────────
    print("\n[2] Classifying day types …")
    mp = classify_days(mp)
    print_day_type_analysis(mp)

    # ── 3. Overnight gaps ─────────────────────────────────────────────────
    print("\n[3] Computing overnight gaps …")
    mp = compute_overnight_gaps(mp)
    print_overnight_gap_analysis(mp)

    # ── 4. VA migration ───────────────────────────────────────────────────
    print("\n[4] Computing Value Area migration …")
    mp = compute_va_migration(mp)
    print_va_migration_analysis(mp)

    # ── 5. Opening type classification ─────────────────────────────────────
    print("\n[5] Classifying opening types …")
    mp = classify_opening_type(mp)

    # ── 6. Expansion scoring ──────────────────────────────────────────────
    print("\n[6] Computing expansion scores …")
    mp = compute_expansion_score(mp)
    print_expansion_score_analysis(mp)

    # ── 7. Carry signals ──────────────────────────────────────────────────
    print("\n[7] Generating carry signals …")
    mp = generate_carry_signals(mp)

    # ── 8. Backtest carry ─────────────────────────────────────────────────
    print("\n[8] Backtesting overnight carry …")
    carry_df = backtest_carry(mp)
    print_carry_backtest(carry_df)

    # ── 9. Predictive analysis ────────────────────────────────────────────
    print("\n[9] Predictive analysis …")
    predictive_analysis(mp)

    # ── 10. Save outputs ──────────────────────────────────────────────────
    print("\n[10] Saving results …")

    # Enriched daily MP
    mp_out = mp.drop(columns=["prev_close", "prev_high", "prev_low", "prev_poc",
                               "prev_vah", "prev_val", "next_open", "next_close",
                               "next_ibh", "next_ibl", "next_high", "next_low",
                               "next_day_type", "next_date"], errors="ignore")
    mp_out.to_csv(OUTPUT_ROOT / "enriched_daily_mp.csv", index=False)
    print(f"    Enriched daily MP: {OUTPUT_ROOT / 'enriched_daily_mp.csv'}")

    # Carry trades
    if len(carry_df) > 0:
        carry_df.to_csv(OUTPUT_ROOT / "carry_trades.csv", index=False)
        print(f"    Carry trades: {OUTPUT_ROOT / 'carry_trades.csv'}")

    # ── 11. Dashboard ─────────────────────────────────────────────────────
    print("\n[11] Generating dashboard …")
    plot_dashboard(mp, carry_df)

    print("\n" + "=" * 72)
    print("  ANALYSIS COMPLETE")
    print("=" * 72)


if __name__ == "__main__":
    run()
