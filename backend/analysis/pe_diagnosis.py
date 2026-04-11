"""
PE Trade Failure Diagnosis — Why don't PEs profit in bearish months?
====================================================================
Analyzes:
1. Monthly spot direction vs PE trade returns
2. Feb 2026 deep-dive: day-by-day choppy pattern
3. Entry timing (TTE), premium level, reversal rate correlation
4. Proposed filters to fix PE entries
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

DATA_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"
UNDERLYING = "SENSEX"

# ── Load MP data for day classification ──────────────────────────────────────
def load_daily_mp():
    path = DATA_ROOT / "market_profile" / "daily_mp_params.csv"
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("poc", "vah", "val", "var", "ibh", "ibl", "ibr",
              "session_high", "session_low", "open_price", "close_price"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)

def classify_day(r) -> str:
    sr = r["session_high"] - r["session_low"]
    ibr = r["ibr"]
    if ibr <= 0 or sr <= 0: return "UNKNOWN"
    rr = sr / ibr
    cp = (r["close_price"] - r["session_low"]) / sr
    ib_up, ib_dn = r["ib_broken_up"], r["ib_broken_dn"]
    if (ib_up != ib_dn) and rr >= 2.0:
        if ib_up and cp >= 0.70: return "TREND_UP"
        if ib_dn and cp <= 0.30: return "TREND_DN"
    if ib_up and ib_dn and rr >= 1.5: return "DOUBLE_DIST"
    if (ib_up != ib_dn) and rr >= 1.2:
        return "NORMAL_VAR_UP" if ib_up else "NORMAL_VAR_DN"
    if r["fa_up"] or r["fa_dn"]: return "FAILED_AUCTION"
    return "NORMAL"

# ── Load S2 baseline trades ─────────────────────────────────────────────────
def load_s2_trades():
    tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
    df = pd.read_csv(tr_path)
    trades = df[(df["underlying"] == UNDERLYING) & (df["strategy"] == "target_50pct")].copy()
    trades["entry_ts"] = pd.to_datetime(trades["entry_time"])
    trades["month"] = trades["entry_ts"].dt.strftime("%Y-%m")
    trades["entry_date"] = trades["entry_ts"].dt.date

    # Load POC alloc
    poc_path = DATA_ROOT / "option_mp" / "final_strategy_trades.csv"
    poc_lookup = {}
    if poc_path.exists():
        df_poc = pd.read_csv(poc_path)
        for _, row in df_poc.iterrows():
            poc_lookup[row["entry_time"]] = row["poc_alloc"]
    trades["alloc"] = trades["entry_time"].map(lambda x: poc_lookup.get(x, 0.20))
    return trades

# ── Load expansion module trades ─────────────────────────────────────────────
def load_expansion_trades():
    path = DATA_ROOT / "expansion" / "expansion_module_trades.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["entry_ts"] = pd.to_datetime(df["entry_time"])
    df["entry_date"] = df["entry_ts"].dt.date
    return df


def run():
    mp = load_daily_mp()
    mp["day_type"] = mp.apply(classify_day, axis=1)
    mp["daily_move"] = mp["close_price"] - mp["open_price"]
    mp["month"] = mp["date"].apply(lambda d: f"{d.year}-{d.month:02d}")
    mp["prev_close"] = mp["close_price"].shift(1)
    mp["day_dir"] = mp["daily_move"].apply(lambda x: "UP" if x > 0 else "DN")

    # Compute reversal rate per month (days where direction flips from prev day)
    mp["prev_dir"] = mp["day_dir"].shift(1)
    mp["reversed"] = mp["day_dir"] != mp["prev_dir"]

    s2 = load_s2_trades()
    exp = load_expansion_trades()

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 1: Monthly spot direction vs PE trade returns
    # ══════════════════════════════════════════════════════════════════════
    print("=" * 100)
    print("  DIAGNOSIS: WHY DON'T PEs PROFIT IN BEARISH MONTHS?")
    print("=" * 100)

    print("\n  SECTION 1 — Monthly Spot Direction vs PE Trade Performance")
    print("  " + "-" * 80)

    # Monthly spot stats
    monthly_spot = mp.groupby("month").agg(
        spot_move=("daily_move", "sum"),
        n_days=("date", "count"),
        n_up=("day_dir", lambda x: (x == "UP").sum()),
        n_dn=("day_dir", lambda x: (x == "DN").sum()),
        reversal_rate=("reversed", lambda x: x.sum() / max(len(x) - 1, 1) * 100),
        avg_abs_move=("daily_move", lambda x: x.abs().mean()),
        trend_dn_days=("day_type", lambda x: (x == "TREND_DN").sum()),
        trend_up_days=("day_type", lambda x: (x == "TREND_UP").sum()),
    ).reset_index()

    # S2 PE trades per month
    s2_pe = s2[s2["option_type"] == "PE"]
    s2_ce = s2[s2["option_type"] == "CE"]

    s2_pe_monthly = s2_pe.groupby("month").agg(
        pe_trades=("blended_return", "count"),
        pe_avg_ret=("blended_return", "mean"),
        pe_wins=("blended_return", lambda x: (x > 0).sum()),
        pe_losses=("blended_return", lambda x: (x < 0).sum()),
        pe_worst=("blended_return", "min"),
        pe_best=("blended_return", "max"),
    ).reset_index()

    s2_ce_monthly = s2_ce.groupby("month").agg(
        ce_trades=("blended_return", "count"),
        ce_avg_ret=("blended_return", "mean"),
    ).reset_index()

    merged = monthly_spot.merge(s2_pe_monthly, on="month", how="left")
    merged = merged.merge(s2_ce_monthly, on="month", how="left")
    merged = merged.fillna(0)

    print(f"\n  {'Month':<9} {'SpotΔ':>8} {'Dir':>5} {'Rev%':>5} {'#PE':>4} {'PE Avg%':>9} "
          f"{'PE W/L':>7} {'PE Worst':>9} {'#CE':>4} {'CE Avg%':>9} {'Verdict':>20}")
    print("  " + "-" * 100)

    for _, r in merged.iterrows():
        spot_dir = "BULL" if r["spot_move"] > 0 else "BEAR"
        pe_wl = f"{int(r['pe_wins'])}/{int(r['pe_losses'])}" if r["pe_trades"] > 0 else "—"
        verdict = ""
        if r["spot_move"] < -500 and r["pe_avg_ret"] < 0:
            verdict = "⚠ BEAR but PE LOST"
        elif r["spot_move"] < -500 and r["pe_avg_ret"] > 0:
            verdict = "✓ BEAR + PE won"
        elif r["spot_move"] > 500 and r["ce_avg_ret"] > 0:
            verdict = "✓ BULL + CE won"

        print(f"  {r['month']:<9} {r['spot_move']:>+8.0f} {spot_dir:>5} {r['reversal_rate']:>4.0f}% "
              f"{int(r['pe_trades']):>4} {r['pe_avg_ret']:>+8.1f}% {pe_wl:>7} {r['pe_worst']:>+8.1f}% "
              f"{int(r['ce_trades']):>4} {r['ce_avg_ret']:>+8.1f}% {verdict:>20}")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 2: Feb 2026 Deep Dive
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n  SECTION 2 — FEB 2026 DEEP DIVE: Why PEs Lost in a -1000pt Month")
    print("  " + "-" * 80)

    feb_days = mp[mp["month"] == "2026-02"].copy()
    if len(feb_days) == 0:
        print("    No Feb 2026 data found")
        return

    total_move = feb_days["daily_move"].sum()
    rev_rate = feb_days["reversed"].sum() / max(len(feb_days) - 1, 1) * 100

    print(f"\n    Net spot move: {total_move:+.0f} pts")
    print(f"    Up days: {(feb_days['day_dir'] == 'UP').sum()}, Down days: {(feb_days['day_dir'] == 'DN').sum()}")
    print(f"    Reversal rate: {rev_rate:.0f}% (direction changes {feb_days['reversed'].sum()} out of {len(feb_days)-1} days)")
    print(f"    Day types: {dict(feb_days['day_type'].value_counts())}")

    print(f"\n    Day-by-day pattern:")
    print(f"    {'Date':<12} {'Dir':>3} {'Move':>7} {'DayType':<16} {'Reversed?':>10} {'Bar'}")
    print("    " + "-" * 75)

    prev_dir = None
    for _, d in feb_days.iterrows():
        bar_len = int(abs(d["daily_move"]) / 100)
        bar = "█" * min(bar_len, 25)
        rev = "↻ YES" if prev_dir and d["day_dir"] != prev_dir else ""
        print(f"    {str(d['date']):<12} {d['day_dir']:>3} {d['daily_move']:>+7.0f} {d['day_type']:<16} {rev:>10} {bar}")
        prev_dir = d["day_dir"]

    # Feb 2026 PE trades from S2
    feb_pe = s2_pe[s2_pe["month"] == "2026-02"]
    print(f"\n    S2 PE trades in Feb 2026: {len(feb_pe)}")
    if len(feb_pe) > 0:
        print(f"\n    {'Entry Date':<12} {'Entry Time':<22} {'EP':>8} {'Exit':>8} {'Ret%':>8} {'Reason':<18} {'Alloc':>6}")
        print("    " + "-" * 85)
        for _, t in feb_pe.iterrows():
            entry_dt = str(t["entry_date"])
            # Calculate TTE
            try:
                exp_dt = pd.Timestamp(t["expiry"]).date()
                tte = (exp_dt - t["entry_date"]).days
            except Exception:
                tte = "?"
            print(f"    {entry_dt:<12} {str(t['entry_time'])[:22]:<22} ₹{t['entry_price']:>7.2f} "
                  f"₹{t['exit_price']:>7.2f} {t['blended_return']:>+7.1f}% {t['exit_reason']:<18} {t['alloc']:>5.2f}  TTE={tte}d")

    # Feb 2026 expansion PE trades
    if len(exp) > 0:
        feb_exp_pe = exp[(exp["month"] == "2026-02") & (exp["option_type"] == "PE")]
        if len(feb_exp_pe) > 0:
            print(f"\n    Expansion Module PE trades in Feb 2026: {len(feb_exp_pe)}")
            for _, t in feb_exp_pe.iterrows():
                print(f"      {t['module']}: {t['signal_date']} EP=₹{t['entry_price']:.2f} "
                      f"Ret={t['blended_return']:+.1f}% {t['exit_reason']}")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 3: Root Cause — Choppy vs Trending Bearish
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n  SECTION 3 — ROOT CAUSE: Choppy Bearish vs Trending Bearish")
    print("  " + "-" * 80)

    # For each month, classify as trending or choppy
    print(f"\n  Bearish Month Analysis:")
    print(f"  {'Month':<9} {'SpotΔ':>8} {'Type':>10} {'Rev%':>6} {'MaxConsecDN':>12} {'PE Avg%':>9} {'PE n':>5}")
    print("  " + "-" * 65)

    for _, r in merged.iterrows():
        if r["spot_move"] >= 0:
            continue

        # Find max consecutive down days
        m_days = mp[mp["month"] == r["month"]]["day_dir"].values
        max_consec_dn = 0
        curr = 0
        for d in m_days:
            if d == "DN":
                curr += 1
                max_consec_dn = max(max_consec_dn, curr)
            else:
                curr = 0

        bear_type = "TRENDING" if r["reversal_rate"] < 45 else "CHOPPY"
        print(f"  {r['month']:<9} {r['spot_move']:>+8.0f} {bear_type:>10} {r['reversal_rate']:>5.0f}% "
              f"{max_consec_dn:>12} {r['pe_avg_ret']:>+8.1f}% {int(r['pe_trades']):>5}")

    print(f"\n  KEY INSIGHT:")
    print(f"  ─────────────")
    print(f"  Options (especially PEs) need SUSTAINED directional moves to profit.")
    print(f"  In choppy-bearish months (reversal rate > 50%), the market oscillates:")
    print(f"    • Down 1000pts → Up 500pts → Down 800pts → Up 600pts …")
    print(f"    • Net: bearish. But PE bought on a down day faces a +500pt rally next day.")
    print(f"    • Theta decay + counter-rallies = PE death in choppy bear markets.")
    print(f"  In trending-bearish months (reversal rate < 45%), PE trades capture sustained drops.")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 4: Entry timing analysis — TTE and premium level
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n  SECTION 4 — ENTRY TIMING: TTE & Premium Level vs Returns")
    print("  " + "-" * 80)

    pe_all = s2_pe.copy()
    pe_all["expiry_dt"] = pd.to_datetime(pe_all["expiry"]).dt.tz_localize(None)
    pe_all["entry_dt"] = pe_all["entry_ts"].dt.tz_localize(None)
    pe_all["tte"] = (pe_all["expiry_dt"] - pe_all["entry_dt"]).dt.days

    print(f"\n    PE trades by TTE (days to expiry):")
    print(f"    {'TTE':>4} {'Count':>6} {'WR%':>6} {'Avg Ret':>9} {'Median':>8}")
    print("    " + "-" * 40)
    for tte_bucket in [0, 1, 2, 3, 4, 5, 6, 7]:
        if tte_bucket < 7:
            sub = pe_all[pe_all["tte"] == tte_bucket]
            label = str(tte_bucket)
        else:
            sub = pe_all[pe_all["tte"] >= 7]
            label = "7+"
        if len(sub) == 0:
            continue
        wr = (sub["blended_return"] > 0).sum() / len(sub) * 100
        avg = sub["blended_return"].mean()
        med = sub["blended_return"].median()
        print(f"    {label:>4} {len(sub):>6} {wr:>5.0f}% {avg:>+8.1f}% {med:>+7.1f}%")

    print(f"\n    PE trades by entry premium level:")
    print(f"    {'Premium':>10} {'Count':>6} {'WR%':>6} {'Avg Ret':>9}")
    print("    " + "-" * 40)
    for lo, hi, label in [(0, 50, "₹0-50"), (50, 100, "₹50-100"), (100, 200, "₹100-200"),
                           (200, 500, "₹200-500"), (500, 9999, "₹500+")]:
        sub = pe_all[(pe_all["entry_price"] >= lo) & (pe_all["entry_price"] < hi)]
        if len(sub) == 0:
            continue
        wr = (sub["blended_return"] > 0).sum() / len(sub) * 100
        avg = sub["blended_return"].mean()
        print(f"    {label:>10} {len(sub):>6} {wr:>5.0f}% {avg:>+8.1f}%")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 5: Trailing reversal rate at entry
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n  SECTION 5 — TRAILING REVERSAL RATE AT PE ENTRY")
    print("  " + "-" * 80)

    # For each PE trade, compute 5-day trailing reversal rate at entry
    mp_dict = {row["date"]: row for _, row in mp.iterrows()}
    sorted_dates = sorted(mp["date"].values)
    date_to_idx = {d: i for i, d in enumerate(sorted_dates)}

    pe_with_context = []
    for _, t in pe_all.iterrows():
        entry_d = t["entry_date"]
        idx = date_to_idx.get(entry_d)
        if idx is None or idx < 5:
            continue

        # Trailing 5-day reversal count
        trail_days = [sorted_dates[idx - i] for i in range(5)]
        trail_dirs = [mp_dict[d]["close_price"] > mp_dict[d]["open_price"] for d in trail_days if d in mp_dict]
        reversals = sum(1 for i in range(1, len(trail_dirs)) if trail_dirs[i] != trail_dirs[i-1])
        rev_rate_5d = reversals / max(len(trail_dirs) - 1, 1) * 100

        # Consecutive down days before entry
        consec_dn = 0
        for i in range(idx, max(idx - 10, -1), -1):
            d = sorted_dates[i]
            if d in mp_dict and mp_dict[d]["close_price"] < mp_dict[d]["open_price"]:
                consec_dn += 1
            else:
                break

        pe_with_context.append({
            "entry_date": entry_d,
            "month": t["month"],
            "entry_price": t["entry_price"],
            "ret": t["blended_return"],
            "tte": t["tte"],
            "rev_rate_5d": rev_rate_5d,
            "consec_dn": consec_dn,
        })

    if pe_with_context:
        pdf = pd.DataFrame(pe_with_context)

        print(f"\n    PE returns by trailing 5-day reversal rate:")
        print(f"    {'RevRate':>8} {'Count':>6} {'WR%':>6} {'Avg Ret':>9}")
        print("    " + "-" * 35)
        for lo, hi, label in [(0, 30, "0-30%"), (30, 50, "30-50%"), (50, 75, "50-75%"), (75, 101, "75-100%")]:
            sub = pdf[(pdf["rev_rate_5d"] >= lo) & (pdf["rev_rate_5d"] < hi)]
            if len(sub) == 0: continue
            wr = (sub["ret"] > 0).sum() / len(sub) * 100
            avg = sub["ret"].mean()
            print(f"    {label:>8} {len(sub):>6} {wr:>5.0f}% {avg:>+8.1f}%")

        print(f"\n    PE returns by consecutive down days before entry:")
        print(f"    {'ConsecDN':>8} {'Count':>6} {'WR%':>6} {'Avg Ret':>9}")
        print("    " + "-" * 35)
        for n in [0, 1, 2, 3]:
            if n < 3:
                sub = pdf[pdf["consec_dn"] == n]
                label = str(n)
            else:
                sub = pdf[pdf["consec_dn"] >= 3]
                label = "3+"
            if len(sub) == 0: continue
            wr = (sub["ret"] > 0).sum() / len(sub) * 100
            avg = sub["ret"].mean()
            print(f"    {label:>8} {len(sub):>6} {wr:>5.0f}% {avg:>+8.1f}%")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 6: Proposed Filters
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n  SECTION 6 — PROPOSED FILTERS TO FIX PE ENTRIES")
    print("  " + "-" * 80)

    # Simulate what happens if we apply filters to PE trades
    filters = {
        "No filter (current)":      lambda t: True,
        "TTE >= 3 days":            lambda t: t["tte"] >= 3,
        "TTE >= 5 days":            lambda t: t["tte"] >= 5,
        "Premium >= ₹50":           lambda t: t["entry_price"] >= 50,
        "Premium >= ₹100":          lambda t: t["entry_price"] >= 100,
        "RevRate5d < 50%":          lambda t: t["rev_rate_5d"] < 50,
        "ConsecDN >= 2":            lambda t: t["consec_dn"] >= 2,
        "TTE≥3 + Premium≥₹50":     lambda t: t["tte"] >= 3 and t["entry_price"] >= 50,
        "TTE≥3 + Rev<50%":         lambda t: t["tte"] >= 3 and t["rev_rate_5d"] < 50,
        "COMBO: TTE≥3 + Prem≥₹50 + Rev<60%": lambda t: t["tte"] >= 3 and t["entry_price"] >= 50 and t["rev_rate_5d"] < 60,
    }

    if pe_with_context:
        print(f"\n    {'Filter':<40} {'Kept':>5} {'Removed':>8} {'WR%':>6} {'Avg%':>8} {'EqΔ':>10}")
        print("    " + "-" * 80)

        for name, filt in filters.items():
            kept = [t for t in pe_with_context if filt(t)]
            removed = len(pe_with_context) - len(kept)
            if len(kept) == 0:
                print(f"    {name:<40} {0:>5} {removed:>8}  —       —")
                continue
            wr = sum(1 for t in kept if t["ret"] > 0) / len(kept) * 100
            avg = np.mean([t["ret"] for t in kept])

            # Simulate equity impact (PE only, 10% alloc for worst case)
            eq = 100_000
            for t in sorted(kept, key=lambda x: str(x["entry_date"])):
                alloc = 0.10  # conservative
                eq = eq + eq * alloc * max(t["ret"], -50) / 100.0
            eq_change = (eq - 100_000) / 100_000 * 100

            print(f"    {name:<40} {len(kept):>5} {removed:>8} {wr:>5.0f}% {avg:>+7.1f}% {eq_change:>+9.1f}%")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 7: What Feb should have looked like
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n  SECTION 7 — FEB 2026: WHAT THE STRATEGY MISSED")
    print("  " + "-" * 80)

    feb_days_detail = mp[mp["month"] == "2026-02"].copy()

    # Find the actual big down-moves in Feb
    big_downs = feb_days_detail[feb_days_detail["daily_move"] < -500]
    print(f"\n    Big down days in Feb 2026 (> 500pt drop): {len(big_downs)}")
    for _, d in big_downs.iterrows():
        next_days = feb_days_detail[feb_days_detail["date"] > d["date"]].head(3)
        next_moves = [f"{nd['daily_move']:+.0f}" for _, nd in next_days.iterrows()]
        print(f"      {d['date']}: {d['daily_move']:+.0f} pts ({d['day_type']})  → next 3 days: {', '.join(next_moves)}")

    print(f"\n    The core problem:")
    print(f"    ─────────────────")
    print(f"    Feb 2026 had {len(big_downs)} big down days (>500pt), but each was followed")
    print(f"    by a counter-rally. A PE entry on a big down day would face immediate")
    print(f"    headwinds the very next session.")

    # Check if any big down day had consecutive follow-through
    print(f"\n    Down-day follow-through in Feb 2026:")
    for _, d in feb_days_detail.iterrows():
        if d["daily_move"] >= 0:
            continue
        idx = date_to_idx.get(d["date"])
        if idx is None or idx + 1 >= len(sorted_dates):
            continue
        next_d = sorted_dates[idx + 1]
        if next_d in mp_dict:
            next_move = mp_dict[next_d]["close_price"] - mp_dict[next_d]["open_price"]
            follow = "CONT ✓" if next_move < 0 else "REVERSED ✗"
            print(f"      {d['date']} {d['daily_move']:>+7.0f} → {next_d} {next_move:>+7.0f}  {follow}")

    print("\n" + "=" * 100)
    print("  DIAGNOSIS COMPLETE")
    print("=" * 100)


if __name__ == "__main__":
    run()
