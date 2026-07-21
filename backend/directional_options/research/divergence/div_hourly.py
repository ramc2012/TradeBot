"""(D, element e) THE HOURLY EARLY CLUE.

Owner: "current move on daily timeframe is just starting but hourly gave early
clues." Three separable questions, all answered here:

  BASE RATE  how often does the hourly MACD cross fire at all? If it fires
             every few sessions then "the hourly led" is nearly vacuous and no
             lead statistic means anything.
  LEAD       sessions from the hourly trigger to the daily crossover.
  ECONOMICS  does ACTING on the hourly beat WAITING for the daily? Both arms
             run through byte-identical barriers (same +2/-1 daily-ATR target
             and stop, same 15-session cutoff); only entry timing differs.

TWO HOURLY ARMS, and the difference between them is the whole honesty story:

  hourly_tradeable  CAUSAL. Enter at the OPEN of the 30m bar after ANY hourly
                    MACD bull cross that occurs while the DAILY divergence
                    state is already true and the daily MACD has NOT yet
                    crossed. This is a rule you could actually have run.
  hourly_oracle     NOT TRADEABLE, reported as an upper bound only. Enter at
                    the hourly cross that immediately precedes the daily cross
                    -- which requires knowing, in advance, which hourly cross
                    was "the one". Any lead number quoted off this arm is
                    hindsight.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "cascade"))
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import div_build as B  # noqa: E402
import div_defs as D  # noqa: E402
import run_cascade as rc  # noqa: E402


def _pos_after(bars, u, sidx, bidx):
    Bu = bars.u.get(u)
    if Bu is None:
        return None
    fb = bars.first_bar.get((u, sidx))
    if fb is None:
        return None
    p = fb + bidx + 1
    sid = Bu["sidx"]
    if p < len(sid) and sid[p] == sidx:
        return p
    return bars.first_bar.get((u, sidx + 1))


def main() -> None:
    daily, intra = B.load_panel()
    e = B.build_daily_elements(daily)
    h = B.build_hourly(intra)
    bars = rc.Bars(intra, daily)
    ep = pd.read_parquet(os.path.join(DATA, "episodes.parquet"))
    real = ep[ep["arm"] == D.PRIMARY_ARM].copy()

    sidx_of = {(r.underlying, r.session): int(r.sidx) for r in e.itertuples()}
    atr_of = {(r.underlying, int(r.sidx)): float(r.D_atr14) for r in e.itertuples()}
    div_of = {(r.underlying, int(r.sidx)): (bool(r.div), float(r.D_macd - r.D_macd_sig))
              for r in e.itertuples()}

    hx = {}
    for u, g in h.groupby("underlying", sort=False):
        g = g[g["H_cross"].fillna(False).to_numpy(bool)]
        arr = [(sidx_of.get((u, s), -1), int(b)) for s, b in zip(g["session"], g["bidx"])]
        hx[u] = sorted([(a, b) for a, b in arr if a >= 0])

    # ---- base rate -------------------------------------------------------
    n_sess = len(e)
    n_hx = sum(len(v) for v in hx.values())
    print(f"hourly MACD bull crosses: {n_hx} over {n_sess} underlying-sessions "
          f"= one every {n_sess / max(n_hx, 1):.2f} sessions per name")
    print(f"daily  MACD bull crosses: {int(e['cross'].sum())} "
          f"= one every {n_sess / max(int(e['cross'].sum()), 1):.2f} sessions per name")

    # ---- arm 1: causal, tradeable ---------------------------------------
    rows = []
    for u, lst in hx.items():
        for a, bidx in lst:
            st = div_of.get((u, a))
            if st is None or not st[0] or st[1] > 0:
                continue          # divergence not yet true, or daily already crossed
            p = _pos_after(bars, u, a, bidx)
            if p is None:
                continue
            atr_h = atr_of.get((u, a - 1), np.nan)
            if not np.isfinite(atr_h) or atr_h <= 0:
                continue
            s = rc.path_stats(bars.u[u], p, D.SIDE, atr_h, a + D.HORIZON_SESSIONS)
            if not s:
                continue
            rows.append({"underlying": u, "sidx_entry": a, "arm": "hourly_tradeable", **s})
    ht = pd.DataFrame(rows)
    if not ht.empty:
        ht = ht.sort_values(["underlying", "sidx_entry"])
        prev = ht.groupby("underlying")["sidx_entry"].shift(1)
        ht = ht[(((ht["sidx_entry"] - prev) > D.EPISODE_GAP_SESSIONS) | prev.isna()).to_numpy()]

    # ---- arm 2: oracle-anchored upper bound ------------------------------
    rows = []
    leads = []
    for r in real.itertuples():
        u, cs = r.underlying, int(r.sidx_cross)
        cand = [(a, b) for a, b in hx.get(u, []) if a <= cs]
        if not cand:
            continue
        a, bidx = cand[-1]
        leads.append(cs - a)
        p = _pos_after(bars, u, a, bidx)
        atr_h = atr_of.get((u, a - 1), np.nan)
        if p is None or not np.isfinite(atr_h) or atr_h <= 0:
            continue
        s = rc.path_stats(bars.u[u], p, D.SIDE, atr_h, a + D.HORIZON_SESSIONS)
        if not s:
            continue
        rows.append({"underlying": u, "sidx_entry": a, "arm": "hourly_oracle",
                     "d_large": r.large, "d_term_atr": r.term_atr,
                     "d_entry": r.entry_spot, "lead": cs - a, "quarter": r.quarter, **s})
    ho = pd.DataFrame(rows)

    leads = np.asarray(leads, float)
    if len(leads):
        print(f"\nORACLE lead to the daily cross (n={len(leads)}): median "
              f"{np.median(leads):.1f} mean {leads.mean():.2f} "
              f"p25 {np.percentile(leads,25):.0f} p75 {np.percentile(leads,75):.0f} "
              f"same-session {100*(leads==0).mean():.1f}%")

    print(f"\nARM                 n     P(large)   term_atr   mfe_atr   mae_atr")
    d = real
    print(f"{'daily cross_div':18s} {len(d):5d} {d['large'].mean():10.3f} "
          f"{d['term_atr'].mean():10.3f} {d['mfe_atr'].mean():9.3f} {d['mae_atr'].mean():9.3f}")
    for nm, df in (("hourly_tradeable", ht), ("hourly_oracle", ho)):
        if df is None or df.empty:
            continue
        print(f"{nm:18s} {len(df):5d} {df['large'].mean():10.3f} "
              f"{df['term_atr'].mean():10.3f} {df['mfe_atr'].mean():9.3f} "
              f"{df['mae_atr'].mean():9.3f}")

    if not ho.empty:
        g = ho
        print(f"\nPAIRED (oracle hourly vs its own daily entry, n={len(g)}):")
        for col in ("large", "term_atr"):
            a, b = g[col].astype(float), g["d_" + col].astype(float)
            dd = a - b
            se = dd.std(ddof=1) / np.sqrt(len(dd))
            print(f"  {col:9s} hourly {a.mean():+.4f} daily {b.mean():+.4f} "
                  f"diff {dd.mean():+.4f} (se {se:.4f}, t {dd.mean()/se:+.2f})")
        adv = 1.0 - g["entry_spot"] / g["d_entry"]
        print(f"  entry advantage of hourly: median {adv.median():+.3%} "
              f"mean {adv.mean():+.3%} (positive = entered cheaper)")

    out = pd.concat([x for x in (ht, ho) if x is not None and not x.empty], ignore_index=True)
    out.to_parquet(os.path.join(DATA, "hourly_arms.parquet"), index=False)
    print("\nwrote hourly_arms.parquet", len(out))


if __name__ == "__main__":
    main()
