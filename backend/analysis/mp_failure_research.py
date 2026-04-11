"""
Market Profile — Buyer/Seller Failure Research
================================================

Core MP principle: Market auctions to find fair value.
When buyers or sellers FAIL to sustain their auction, the opposite side takes over.

This script identifies and quantifies all buyer/seller failure patterns
from SENSEX daily MP data + 1-min spot data.

Failure Signals Researched:
  1. FAILED AUCTION (FA)         — IB extension that closes back inside IB
  2. IB EXTENSION FAILURE        — IB broken in one direction, close in opposite half
  3. POOR HIGH / POOR LOW        — Single-TPO extreme (no excess/tail)
  4. VA REJECTION                — Opens outside prev VA, closes back inside
  5. EXCESS TAIL                 — Long tail at extreme (sellers/buyers firmly rejected)
  6. INITIATIVE FAILURE          — Opens with drive, fails to extend
  7. RESPONSIVE FAILURE          — Price enters prev VA, fails to reach POC
  8. COMPOSITE: WHO FAILED?      — Combine signals into buyer_failed / seller_failed score

For each signal: what happens next day? Next week? What's the option trade edge?
"""
from __future__ import annotations

import gzip, json, math
from collections import defaultdict
from pathlib import Path

import pandas as pd
import numpy as np

DATA_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"
UNDERLYING = "SENSEX"
BUCKET_SIZE = 50
TPO_MINUTES = 30

# ── Load data ────────────────────────────────────────────────────────────────

def load_spot_1m():
    path = DATA_ROOT / f"spot/underlying={UNDERLYING}/1minute.csv.gz"
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

def load_mp():
    path = DATA_ROOT / "market_profile" / "daily_mp_params.csv"
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("poc", "vah", "val", "var", "ibh", "ibl", "ibr",
              "session_high", "session_low", "open_price", "close_price"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)

def _bucket(price):
    return math.floor(price / BUCKET_SIZE) * BUCKET_SIZE


# ── Compute extended MP metrics from 1-min data ─────────────────────────────

def compute_extended_mp(spot_1m: pd.DataFrame, mp: pd.DataFrame) -> pd.DataFrame:
    """
    Enrich daily MP with:
    - TPO profile shape (poor high, poor low, excess tails)
    - Close position within range and VA
    - Opening type context
    - IB extension details
    """
    spot_by_date = {d: g.reset_index(drop=True) for d, g in spot_1m.groupby(spot_1m["time"].dt.date)}

    records = []
    for idx, row in mp.iterrows():
        d = row["date"]
        day_df = spot_by_date.get(d)
        if day_df is None or len(day_df) < 120:
            continue

        r = dict(row)
        session_range = row["session_high"] - row["session_low"]
        if session_range <= 0:
            continue

        ibr = row["ibr"]
        ibh = row["ibh"]
        ibl = row["ibl"]

        # ── Close position metrics ────────────────────────────────────────
        r["close_pct_range"] = (row["close_price"] - row["session_low"]) / session_range
        r["close_pct_ib"] = (row["close_price"] - ibl) / ibr if ibr > 0 else 0.5
        r["range_to_ibr"] = session_range / ibr if ibr > 0 else 1.0

        # ── Close relative to VA ──────────────────────────────────────────
        r["close_above_vah"] = row["close_price"] > row["vah"]
        r["close_below_val"] = row["close_price"] < row["val"]
        r["close_in_va"] = not r["close_above_vah"] and not r["close_below_val"]

        # ── TPO profile for tails ─────────────────────────────────────────
        tpo_counts = defaultdict(int)
        tpo_periods = day_df.set_index("time").resample(f"{TPO_MINUTES}min",
                      label="left", closed="left").agg({"high": "max", "low": "min"}).dropna()
        for _, trow in tpo_periods.iterrows():
            lo_b = _bucket(float(trow["low"]))
            hi_b = _bucket(float(trow["high"]))
            b = lo_b
            while b <= hi_b:
                tpo_counts[b] += 1
                b += BUCKET_SIZE

        if not tpo_counts:
            continue

        sorted_buckets = sorted(tpo_counts.keys())
        max_tpo = max(tpo_counts.values())

        # Poor High: top 2 buckets have <= 2 TPOs (no buying conviction at top)
        top_2 = sorted_buckets[-2:] if len(sorted_buckets) >= 2 else sorted_buckets[-1:]
        top_tpos = sum(tpo_counts[b] for b in top_2)
        r["poor_high"] = top_tpos <= 2

        # Poor Low: bottom 2 buckets have <= 2 TPOs (no selling conviction at bottom)
        bot_2 = sorted_buckets[:2] if len(sorted_buckets) >= 2 else sorted_buckets[:1]
        bot_tpos = sum(tpo_counts[b] for b in bot_2)
        r["poor_low"] = bot_tpos <= 2

        # Excess tail at high (buying tail): top buckets with single TPOs
        tail_high = 0
        for b in reversed(sorted_buckets):
            if tpo_counts[b] <= 1:
                tail_high += 1
            else:
                break
        r["tail_high_buckets"] = tail_high
        r["excess_high"] = tail_high >= 3  # 3+ single-TPO buckets = strong rejection

        # Excess tail at low (selling tail): bottom buckets with single TPOs
        tail_low = 0
        for b in sorted_buckets:
            if tpo_counts[b] <= 1:
                tail_low += 1
            else:
                break
        r["tail_low_buckets"] = tail_low
        r["excess_low"] = tail_low >= 3

        # ── IB Extension Failure ──────────────────────────────────────────
        # Buyers broke above IBH but couldn't close above IB midpoint
        ib_mid = (ibh + ibl) / 2
        r["ib_ext_up_fail"] = row["ib_broken_up"] and row["close_price"] < ib_mid
        r["ib_ext_dn_fail"] = row["ib_broken_dn"] and row["close_price"] > ib_mid

        # Stronger version: broke IB but closed on the opposite side
        r["ib_ext_up_reversal"] = row["ib_broken_up"] and row["close_price"] < ibl
        r["ib_ext_dn_reversal"] = row["ib_broken_dn"] and row["close_price"] > ibh

        # ── Opening context ───────────────────────────────────────────────
        open_p = row["open_price"]
        # Previous day's VA (will be set in the enrichment loop below)
        r["open_above_prev_vah"] = False  # placeholder
        r["open_below_prev_val"] = False
        r["open_in_prev_va"] = False

        # ── Daily move ────────────────────────────────────────────────────
        r["daily_move"] = row["close_price"] - row["open_price"]
        r["daily_pct"] = r["daily_move"] / row["open_price"] * 100

        # ── First 30 min and last 30 min ──────────────────────────────────
        first_30 = day_df.iloc[:30]
        last_30 = day_df.iloc[-30:]
        r["first_30_move"] = float(first_30["close"].iloc[-1]) - float(first_30["open"].iloc[0])
        r["last_30_move"] = float(last_30["close"].iloc[-1]) - float(last_30["open"].iloc[0])
        # Closing rally/selloff: last 30 min direction vs day direction
        r["close_confirms"] = (r["last_30_move"] > 0) == (r["daily_move"] > 0)

        records.append(r)

    edf = pd.DataFrame(records)

    # ── Enrich with previous-day context ──────────────────────────────────
    edf = edf.sort_values("date").reset_index(drop=True)
    for i in range(1, len(edf)):
        prev = edf.iloc[i - 1]
        curr_open = edf.at[i, "open_price"]
        edf.at[i, "prev_vah"] = prev["vah"]
        edf.at[i, "prev_val"] = prev["val"]
        edf.at[i, "prev_poc"] = prev["poc"]
        edf.at[i, "prev_close"] = prev["close_price"]
        edf.at[i, "prev_high"] = prev["session_high"]
        edf.at[i, "prev_low"] = prev["session_low"]
        edf.at[i, "open_above_prev_vah"] = curr_open > prev["vah"]
        edf.at[i, "open_below_prev_val"] = curr_open < prev["val"]
        edf.at[i, "open_in_prev_va"] = prev["val"] <= curr_open <= prev["vah"]

        # VA Rejection: opened outside prev VA, closed back inside
        edf.at[i, "va_reject_from_above"] = (curr_open > prev["vah"] and
                                               edf.at[i, "close_price"] <= prev["vah"])
        edf.at[i, "va_reject_from_below"] = (curr_open < prev["val"] and
                                               edf.at[i, "close_price"] >= prev["val"])

        # Responsive failure: entered prev VA but couldn't reach prev POC
        edf.at[i, "resp_fail_from_above"] = (curr_open > prev["vah"] and
                                               edf.at[i, "session_low"] > prev["poc"])
        edf.at[i, "resp_fail_from_below"] = (curr_open < prev["val"] and
                                               edf.at[i, "session_high"] < prev["poc"])

        # Gap info
        edf.at[i, "gap"] = curr_open - prev["close_price"]
        edf.at[i, "gap_pct"] = edf.at[i, "gap"] / prev["close_price"] * 100
        # Gap fill: did the session fill the gap?
        if edf.at[i, "gap"] > 0:  # gap up
            edf.at[i, "gap_filled"] = edf.at[i, "session_low"] <= prev["close_price"]
        elif edf.at[i, "gap"] < 0:  # gap down
            edf.at[i, "gap_filled"] = edf.at[i, "session_high"] >= prev["close_price"]
        else:
            edf.at[i, "gap_filled"] = True

    # ── Next-day outcome ──────────────────────────────────────────────────
    edf["next_day_move"] = edf["daily_move"].shift(-1)
    edf["next_day_pct"] = edf["daily_pct"].shift(-1)
    edf["next_day_open"] = edf["open_price"].shift(-1)
    edf["next_day_close"] = edf["close_price"].shift(-1)

    # Next 3-day cumulative move
    edf["next_3d_move"] = edf["daily_move"].shift(-1) + edf["daily_move"].shift(-2) + edf["daily_move"].shift(-3)

    return edf


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def analyze_signal(edf, mask, signal_name, expected_next_dir=""):
    """Analyze a boolean mask signal's predictive power on next-day move."""
    sub = edf[mask].dropna(subset=["next_day_move"])
    n = len(sub)
    if n == 0:
        return None

    next_moves = sub["next_day_move"].values
    next_3d = sub["next_3d_move"].dropna().values

    up_next = (next_moves > 0).sum()
    dn_next = (next_moves < 0).sum()
    avg_next = np.mean(next_moves)
    med_next = np.median(next_moves)
    avg_3d = np.mean(next_3d) if len(next_3d) > 0 else 0

    # Win rate in expected direction
    if expected_next_dir == "UP":
        wr = up_next / n * 100
    elif expected_next_dir == "DN":
        wr = dn_next / n * 100
    else:
        wr = max(up_next, dn_next) / n * 100

    return {
        "signal": signal_name,
        "n": n,
        "expected": expected_next_dir,
        "wr_expected": round(wr, 1),
        "next_up": up_next,
        "next_dn": dn_next,
        "avg_next_move": round(avg_next, 0),
        "med_next_move": round(med_next, 0),
        "avg_3d_move": round(avg_3d, 0),
    }


def run():
    print("=" * 100)
    print("  MARKET PROFILE — BUYER/SELLER FAILURE RESEARCH")
    print("  SENSEX | 246 Trading Days | Apr'25 — Apr'26")
    print("=" * 100)

    print("\n[1] Loading 1-min spot data + daily MP params …")
    spot_1m = load_spot_1m()
    mp = load_mp()
    print(f"    {len(spot_1m)} spot candles, {len(mp)} MP days")

    print("\n[2] Computing extended MP metrics …")
    edf = compute_extended_mp(spot_1m, mp)
    print(f"    {len(edf)} enriched days")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION A: Individual Failure Signals
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("  SECTION A — INDIVIDUAL BUYER/SELLER FAILURE SIGNALS")
    print("=" * 100)

    signals = []

    # 1. Failed Auction Up → Buyers failed → expect DOWN
    signals.append(analyze_signal(edf, edf["fa_up"] == True, "FA_UP (buyers failed)", "DN"))
    # 2. Failed Auction Down → Sellers failed → expect UP
    signals.append(analyze_signal(edf, edf["fa_dn"] == True, "FA_DN (sellers failed)", "UP"))

    # 3. IB Extension Up Failure → Buyers broke out then failed → expect DOWN
    signals.append(analyze_signal(edf, edf["ib_ext_up_fail"] == True, "IB_EXT_UP_FAIL (buyers broke IB, closed below mid)", "DN"))
    # 4. IB Extension Down Failure → Sellers broke out then failed → expect UP
    signals.append(analyze_signal(edf, edf["ib_ext_dn_fail"] == True, "IB_EXT_DN_FAIL (sellers broke IB, closed above mid)", "UP"))

    # 5. IB Extension Reversal (extreme)
    signals.append(analyze_signal(edf, edf["ib_ext_up_reversal"] == True, "IB_EXT_UP_REVERSAL (broke IBH, closed below IBL)", "DN"))
    signals.append(analyze_signal(edf, edf["ib_ext_dn_reversal"] == True, "IB_EXT_DN_REVERSAL (broke IBL, closed above IBH)", "UP"))

    # 6. Poor High → Buyers couldn't build value at top → expect DOWN
    signals.append(analyze_signal(edf, edf["poor_high"] == True, "POOR_HIGH (no buying conviction at top)", "DN"))
    # 7. Poor Low → Sellers couldn't build value at bottom → expect UP
    signals.append(analyze_signal(edf, edf["poor_low"] == True, "POOR_LOW (no selling conviction at bottom)", "UP"))

    # 8. Excess tail at high → Sellers firmly rejected buyers → expect DOWN
    signals.append(analyze_signal(edf, edf["excess_high"] == True, "EXCESS_HIGH (strong seller rejection at top)", "DN"))
    # 9. Excess tail at low → Buyers firmly rejected sellers → expect UP
    signals.append(analyze_signal(edf, edf["excess_low"] == True, "EXCESS_LOW (strong buyer rejection at bottom)", "UP"))

    # 10. VA Rejection from above → Buyers tried to hold above VA, failed → expect DOWN
    va_rej_above = edf.get("va_reject_from_above")
    if va_rej_above is not None:
        signals.append(analyze_signal(edf, va_rej_above == True, "VA_REJECT_FROM_ABOVE (opened above VA, closed back in)", "DN"))
    va_rej_below = edf.get("va_reject_from_below")
    if va_rej_below is not None:
        signals.append(analyze_signal(edf, va_rej_below == True, "VA_REJECT_FROM_BELOW (opened below VA, closed back in)", "UP"))

    # 11. Responsive failure — couldn't reach POC
    resp_above = edf.get("resp_fail_from_above")
    if resp_above is not None:
        signals.append(analyze_signal(edf, resp_above == True, "RESP_FAIL_FROM_ABOVE (couldn't reach POC from above)", "UP"))
    resp_below = edf.get("resp_fail_from_below")
    if resp_below is not None:
        signals.append(analyze_signal(edf, resp_below == True, "RESP_FAIL_FROM_BELOW (couldn't reach POC from below)", "DN"))

    # 12. Close confirms direction (momentum) vs close contradicts (reversal signal)
    # Close in top 20% of range on a down day = sellers failed to hold
    seller_fail_close = (edf["daily_move"] < 0) & (edf["close_pct_range"] > 0.70)
    signals.append(analyze_signal(edf, seller_fail_close, "SELLER_FAIL_CLOSE (down day, close in top 30%)", "UP"))
    buyer_fail_close = (edf["daily_move"] > 0) & (edf["close_pct_range"] < 0.30)
    signals.append(analyze_signal(edf, buyer_fail_close, "BUYER_FAIL_CLOSE (up day, close in bottom 30%)", "DN"))

    # 13. Gap up that fills → buyers couldn't sustain → DOWN
    gap_up_filled = (edf.get("gap", pd.Series(dtype=float)) > 100) & (edf.get("gap_filled", pd.Series(dtype=bool)) == True)
    signals.append(analyze_signal(edf, gap_up_filled, "GAP_UP_FILLED (>100pt gap up that fills)", "DN"))
    gap_dn_filled = (edf.get("gap", pd.Series(dtype=float)) < -100) & (edf.get("gap_filled", pd.Series(dtype=bool)) == True)
    signals.append(analyze_signal(edf, gap_dn_filled, "GAP_DN_FILLED (>100pt gap dn that fills)", "UP"))

    # 14. Late-session reversal: last 30 min opposes day direction
    late_reversal_bull = (edf["daily_move"] < 0) & (edf["last_30_move"] > 0) & (edf["last_30_move"].abs() > edf["daily_move"].abs() * 0.3)
    signals.append(analyze_signal(edf, late_reversal_bull, "LATE_REVERSAL_BULL (down day, strong buying close)", "UP"))
    late_reversal_bear = (edf["daily_move"] > 0) & (edf["last_30_move"] < 0) & (edf["last_30_move"].abs() > edf["daily_move"].abs() * 0.3)
    signals.append(analyze_signal(edf, late_reversal_bear, "LATE_REVERSAL_BEAR (up day, strong selling close)", "DN"))

    # Print results
    signals = [s for s in signals if s is not None]

    print(f"\n  {'Signal':<60} {'n':>4} {'Exp':>4} {'WR%':>6} {'AvgΔ':>7} {'MedΔ':>7} {'3dΔ':>7}")
    print("  " + "-" * 100)
    for s in sorted(signals, key=lambda x: -x["wr_expected"]):
        print(f"  {s['signal']:<60} {s['n']:>4} {s['expected']:>4} {s['wr_expected']:>5.1f}% "
              f"{s['avg_next_move']:>+6.0f} {s['med_next_move']:>+6.0f} {s['avg_3d_move']:>+6.0f}")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION B: Composite Buyer/Seller Failure Score
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 100)
    print("  SECTION B — COMPOSITE BUYER/SELLER FAILURE SCORE")
    print("=" * 100)

    # Build a daily score: positive = buyer_failed (expect DOWN), negative = seller_failed (expect UP)
    edf["buyer_fail_score"] = 0.0
    edf["seller_fail_score"] = 0.0

    # Each signal contributes to the score
    # Buyer failed signals (expect DOWN next day)
    edf.loc[edf["fa_up"] == True, "buyer_fail_score"] += 2
    edf.loc[edf["ib_ext_up_fail"] == True, "buyer_fail_score"] += 2
    edf.loc[edf["ib_ext_up_reversal"] == True, "buyer_fail_score"] += 3
    edf.loc[edf["poor_high"] == True, "buyer_fail_score"] += 1
    edf.loc[edf["excess_high"] == True, "buyer_fail_score"] += 1
    edf.loc[(edf["daily_move"] > 0) & (edf["close_pct_range"] < 0.30), "buyer_fail_score"] += 2
    if "va_reject_from_above" in edf.columns:
        edf.loc[edf["va_reject_from_above"] == True, "buyer_fail_score"] += 2
    edf.loc[(edf["daily_move"] > 0) & (edf["last_30_move"] < 0) &
            (edf["last_30_move"].abs() > edf["daily_move"].abs() * 0.3), "buyer_fail_score"] += 1

    # Seller failed signals (expect UP next day)
    edf.loc[edf["fa_dn"] == True, "seller_fail_score"] += 2
    edf.loc[edf["ib_ext_dn_fail"] == True, "seller_fail_score"] += 2
    edf.loc[edf["ib_ext_dn_reversal"] == True, "seller_fail_score"] += 3
    edf.loc[edf["poor_low"] == True, "seller_fail_score"] += 1
    edf.loc[edf["excess_low"] == True, "seller_fail_score"] += 1
    edf.loc[(edf["daily_move"] < 0) & (edf["close_pct_range"] > 0.70), "seller_fail_score"] += 2
    if "va_reject_from_below" in edf.columns:
        edf.loc[edf["va_reject_from_below"] == True, "seller_fail_score"] += 2
    edf.loc[(edf["daily_move"] < 0) & (edf["last_30_move"] > 0) &
            (edf["last_30_move"].abs() > edf["daily_move"].abs() * 0.3), "seller_fail_score"] += 1

    edf["net_failure"] = edf["buyer_fail_score"] - edf["seller_fail_score"]
    # Positive net_failure = buyers failed more → expect DOWN → PE
    # Negative net_failure = sellers failed more → expect UP → CE

    print(f"\n  Score distribution:")
    print(f"    Buyer failure days (score ≥ 2):  {(edf['buyer_fail_score'] >= 2).sum()}")
    print(f"    Seller failure days (score ≥ 2): {(edf['seller_fail_score'] >= 2).sum()}")
    print(f"    Neutral (both < 2):              {((edf['buyer_fail_score'] < 2) & (edf['seller_fail_score'] < 2)).sum()}")
    print(f"    Conflicting (both ≥ 2):          {((edf['buyer_fail_score'] >= 2) & (edf['seller_fail_score'] >= 2)).sum()}")

    # Analyze composite score buckets
    print(f"\n  Composite score vs next-day outcome:")
    print(f"  {'Net Score':>10} {'n':>4} {'Next UP%':>8} {'Avg Next':>9} {'3d Avg':>8} {'Interpretation':<30}")
    print("  " + "-" * 80)

    for lo, hi, label in [(-99, -4, "≤-4"), (-4, -2, "-3 to -2"), (-2, -1, "-1"),
                           (-1, 0, "0"), (0, 1, "+1"), (1, 3, "+2 to +3"), (3, 99, "≥+4")]:
        # net_failure > 0 means buyer failed → expect DOWN
        # net_failure < 0 means seller failed → expect UP
        if lo == -1 and hi == 0:
            sub = edf[edf["net_failure"] == 0].dropna(subset=["next_day_move"])
        elif lo == 0 and hi == 1:
            sub = edf[edf["net_failure"] == 1].dropna(subset=["next_day_move"])
        else:
            sub = edf[(edf["net_failure"] >= lo) & (edf["net_failure"] < hi)].dropna(subset=["next_day_move"])
        n = len(sub)
        if n == 0: continue
        up_pct = (sub["next_day_move"] > 0).sum() / n * 100
        avg_next = sub["next_day_move"].mean()
        avg_3d = sub["next_3d_move"].dropna().mean()
        interp = ""
        if lo <= -2:
            interp = "SELLER FAILED → expect UP"
        elif hi >= 3:
            interp = "BUYER FAILED → expect DN"
        print(f"  {label:>10} {n:>4} {up_pct:>7.1f}% {avg_next:>+8.0f} {avg_3d:>+7.0f}  {interp}")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION C: Buyer/Seller Failure Mapped to S2 Trade Outcomes
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 100)
    print("  SECTION C — FAILURE SIGNALS vs ACTUAL S2 TRADE OUTCOMES")
    print("=" * 100)

    # Load S2 trades
    tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
    df_tr = pd.read_csv(tr_path)
    s2 = df_tr[(df_tr["underlying"] == UNDERLYING) & (df_tr["strategy"] == "target_50pct")].copy()
    s2["entry_dt"] = pd.to_datetime(s2["entry_time"]).dt.tz_localize(None)
    s2["entry_date"] = s2["entry_dt"].dt.date

    # Join with failure scores
    edf_lookup = edf.set_index("date")

    for _, trade in s2.iterrows():
        d = trade["entry_date"]
        if d in edf_lookup.index:
            s2.loc[trade.name, "buyer_fail"] = edf_lookup.at[d, "buyer_fail_score"]
            s2.loc[trade.name, "seller_fail"] = edf_lookup.at[d, "seller_fail_score"]
            s2.loc[trade.name, "net_failure"] = edf_lookup.at[d, "net_failure"]
        else:
            s2.loc[trade.name, "buyer_fail"] = 0
            s2.loc[trade.name, "seller_fail"] = 0
            s2.loc[trade.name, "net_failure"] = 0

    # CE trades: should benefit when seller_fail is high (expect UP)
    ce = s2[s2["option_type"] == "CE"]
    pe = s2[s2["option_type"] == "PE"]

    print(f"\n  CE TRADES — Do they profit when sellers fail?")
    print(f"  {'Seller Fail Score':>18} {'n':>4} {'WR%':>6} {'Avg Ret':>9} {'Catastrophic':>12}")
    print("  " + "-" * 55)
    for lo, hi, label in [(0, 1, "0"), (1, 2, "1"), (2, 4, "2-3"), (4, 99, "4+")]:
        sub = ce[(ce["seller_fail"] >= lo) & (ce["seller_fail"] < hi)]
        if len(sub) == 0: continue
        wr = (sub["blended_return"] > 0).sum() / len(sub) * 100
        avg = sub["blended_return"].mean()
        cat = (sub["blended_return"] < -50).sum()
        print(f"  {label:>18} {len(sub):>4} {wr:>5.1f}% {avg:>+8.1f}% {cat:>12}")

    print(f"\n  PE TRADES — Do they profit when buyers fail?")
    print(f"  {'Buyer Fail Score':>18} {'n':>4} {'WR%':>6} {'Avg Ret':>9} {'Catastrophic':>12}")
    print("  " + "-" * 55)
    for lo, hi, label in [(0, 1, "0"), (1, 2, "1"), (2, 4, "2-3"), (4, 99, "4+")]:
        sub = pe[(pe["buyer_fail"] >= lo) & (pe["buyer_fail"] < hi)]
        if len(sub) == 0: continue
        wr = (sub["blended_return"] > 0).sum() / len(sub) * 100
        avg = sub["blended_return"].mean()
        cat = (sub["blended_return"] < -50).sum()
        print(f"  {label:>18} {len(sub):>4} {wr:>5.1f}% {avg:>+8.1f}% {cat:>12}")

    # The KEY question: what about PE trades where buyers DIDN'T fail?
    print(f"\n  PE TRADES entered when buyers were SUCCEEDING (buyer_fail=0):")
    bad_pe = pe[pe["buyer_fail"] == 0]
    if len(bad_pe) > 0:
        print(f"    n={len(bad_pe)}, WR={((bad_pe['blended_return'] > 0).sum() / len(bad_pe) * 100):.1f}%, "
              f"Avg={bad_pe['blended_return'].mean():+.1f}%, "
              f"Catastrophic={((bad_pe['blended_return'] < -50).sum())}")
        print(f"\n    These are the trades that should have been SKIPPED or had a STOP LOSS:")
        for _, t in bad_pe[bad_pe["blended_return"] < -50].iterrows():
            print(f"      {t['entry_date']} {t['option_type']} EP=₹{t['entry_price']:.0f} "
                  f"Ret={t['blended_return']:+.1f}% → buyers weren't failing, PE had no edge")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION D: Conflict signals — same day has buyer AND seller failure
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 100)
    print("  SECTION D — CONFLICT DAYS & SKIP SIGNAL")
    print("=" * 100)

    conflict = edf[(edf["buyer_fail_score"] >= 2) & (edf["seller_fail_score"] >= 2)]
    non_conflict = edf[~((edf["buyer_fail_score"] >= 2) & (edf["seller_fail_score"] >= 2))]

    print(f"\n  Conflict days (both sides failing): {len(conflict)}")
    if len(conflict) > 0:
        conf_trades = s2[s2["entry_date"].isin(conflict["date"].values)]
        if len(conf_trades) > 0:
            wr = (conf_trades["blended_return"] > 0).sum() / len(conf_trades) * 100
            avg = conf_trades["blended_return"].mean()
            cat = (conf_trades["blended_return"] < -50).sum()
            print(f"    Trades on conflict days: n={len(conf_trades)}, WR={wr:.1f}%, "
                  f"Avg={avg:+.1f}%, Catastrophic={cat}")
            print(f"    → These days are CHOPPY — both sides trying and failing = NO EDGE")

    print(f"\n  Non-conflict days: {len(non_conflict)}")
    nc_trades = s2[s2["entry_date"].isin(non_conflict["date"].values)]
    if len(nc_trades) > 0:
        wr = (nc_trades["blended_return"] > 0).sum() / len(nc_trades) * 100
        avg = nc_trades["blended_return"].mean()
        cat = (nc_trades["blended_return"] < -50).sum()
        print(f"    Trades on non-conflict days: n={len(nc_trades)}, WR={wr:.1f}%, "
              f"Avg={avg:+.1f}%, Catastrophic={cat}")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION E: Feb 2026 re-examined through failure lens
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 100)
    print("  SECTION E — FEB 2026 THROUGH THE FAILURE LENS")
    print("=" * 100)

    feb = edf[edf["date"].apply(lambda d: d.year == 2026 and d.month == 2)]
    print(f"\n  {'Date':<12} {'Move':>7} {'BuyFail':>8} {'SellFail':>9} {'Net':>5} {'Interp':<35} {'PoorH':>6} {'PoorL':>6} {'FA↑':>4} {'FA↓':>4}")
    print("  " + "-" * 110)
    for _, d in feb.iterrows():
        net = d["buyer_fail_score"] - d["seller_fail_score"]
        if net > 1:
            interp = "BUYER FAILED → PE edge"
        elif net < -1:
            interp = "SELLER FAILED → CE edge"
        elif d["buyer_fail_score"] >= 2 and d["seller_fail_score"] >= 2:
            interp = "CONFLICT → SKIP"
        else:
            interp = "neutral"
        print(f"  {str(d['date']):<12} {d['daily_move']:>+7.0f} {d['buyer_fail_score']:>8.0f} "
              f"{d['seller_fail_score']:>9.0f} {net:>+5.0f} {interp:<35} "
              f"{'Y' if d['poor_high'] else '':>6} {'Y' if d['poor_low'] else '':>6} "
              f"{'Y' if d['fa_up'] else '':>4} {'Y' if d['fa_dn'] else '':>4}")

    # Show Feb PE trades with their failure context
    feb_pe = pe[pe["entry_date"].apply(lambda d: d.year == 2026 and d.month == 2)]
    if len(feb_pe) > 0:
        print(f"\n  Feb 2026 PE trades with failure context:")
        for _, t in feb_pe.iterrows():
            bf = t.get("buyer_fail", 0)
            sf = t.get("seller_fail", 0)
            verdict = ""
            if bf < 2:
                verdict = "← SHOULD HAVE SKIPPED (buyers not failing)"
            elif sf >= 2:
                verdict = "← CONFLICT DAY (both failing)"
            else:
                verdict = "← VALID (buyers failing)"
            print(f"    {t['entry_date']} EP=₹{t['entry_price']:.0f} Ret={t['blended_return']:+.1f}% "
                  f"BuyFail={bf:.0f} SellFail={sf:.0f} {verdict}")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION F: SUMMARY & STRATEGY RULES
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 100)
    print("  SECTION F — STRATEGY RULES FROM FAILURE ANALYSIS")
    print("=" * 100)

    # Overall: how much do we gain by only trading when the opposite side fails?
    # CE trades where seller_fail >= 2
    ce_aligned = ce[ce["seller_fail"] >= 2]
    ce_unaligned = ce[ce["seller_fail"] < 2]
    pe_aligned = pe[pe["buyer_fail"] >= 2]
    pe_unaligned = pe[pe["buyer_fail"] < 2]

    print(f"\n  ALIGNED vs UNALIGNED trades (failure confirms trade direction):")
    print(f"  {'Category':<30} {'n':>4} {'WR%':>6} {'Avg Ret':>9} {'Cat(-50%)':>10}")
    print("  " + "-" * 65)
    for label, sub in [("CE aligned (seller fail≥2)", ce_aligned),
                        ("CE unaligned (seller fail<2)", ce_unaligned),
                        ("PE aligned (buyer fail≥2)", pe_aligned),
                        ("PE unaligned (buyer fail<2)", pe_unaligned)]:
        if len(sub) == 0: continue
        wr = (sub["blended_return"] > 0).sum() / len(sub) * 100
        avg = sub["blended_return"].mean()
        cat = (sub["blended_return"] < -50).sum()
        print(f"  {label:<30} {len(sub):>4} {wr:>5.1f}% {avg:>+8.1f}% {cat:>10}")

    # Skip conflicting days
    conflict_dates = set(conflict["date"].values)
    s2_no_conflict = s2[~s2["entry_date"].isin(conflict_dates)]
    s2_conflict = s2[s2["entry_date"].isin(conflict_dates)]

    print(f"\n  SKIP CONFLICT DAYS:")
    if len(s2_conflict) > 0:
        wr_c = (s2_conflict["blended_return"] > 0).sum() / len(s2_conflict) * 100
        avg_c = s2_conflict["blended_return"].mean()
        print(f"    Conflict day trades:     n={len(s2_conflict)}, WR={wr_c:.1f}%, Avg={avg_c:+.1f}%")
    if len(s2_no_conflict) > 0:
        wr_nc = (s2_no_conflict["blended_return"] > 0).sum() / len(s2_no_conflict) * 100
        avg_nc = s2_no_conflict["blended_return"].mean()
        print(f"    Non-conflict day trades: n={len(s2_no_conflict)}, WR={wr_nc:.1f}%, Avg={avg_nc:+.1f}%")

    # Save enriched data
    out_path = DATA_ROOT / "market_profile" / "enriched_mp_with_failures.csv"
    save_cols = ["date", "poc", "vah", "val", "ibh", "ibl", "ibr",
                 "open_price", "close_price", "session_high", "session_low",
                 "daily_move", "daily_pct", "close_pct_range",
                 "fa_up", "fa_dn", "ib_broken_up", "ib_broken_dn",
                 "ib_ext_up_fail", "ib_ext_dn_fail", "ib_ext_up_reversal", "ib_ext_dn_reversal",
                 "poor_high", "poor_low", "excess_high", "excess_low",
                 "tail_high_buckets", "tail_low_buckets",
                 "buyer_fail_score", "seller_fail_score", "net_failure",
                 "next_day_move", "next_3d_move"]
    save_cols = [c for c in save_cols if c in edf.columns]
    edf[save_cols].to_csv(out_path, index=False)
    print(f"\n  Saved: {out_path}")

    print("\n" + "=" * 100)
    print("  RESEARCH COMPLETE")
    print("=" * 100)


if __name__ == "__main__":
    run()
