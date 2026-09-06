"""REFUTATION of mp_time_stability.py.

Attack list:
 A. Look-ahead in the ONE significant directional result (the drawdown bucket
    is stamped from NIFTY's SAME-SESSION close, unknown at the 10:15 break).
 B. Is the at-highs tilt anything other than the same drift the report itself
    used to kill the ret_3d gap?
 C. Serial dependence: buckets are contiguous regimes, the t assumes iid days.
 D. Is the /atr20 "stability" real, or is atr20 contaminated by the same
    session it is normalising, plus a vol/vol tautology?
 E. NaN handling bug in the bucket assignment.
 F. Costs.
"""
from __future__ import annotations
import os, sys, warnings
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.refute_cache import get, INDICES
from research.mp_time_stability import (t_of, summarise, split_half, drop_two_best,
                                        nonoverlap_t, two_sample, cohort_row, table,
                                        per_session_break_bias, per_session_up_minus_down)
warnings.filterwarnings("ignore")
PCT = 100.0
pd.set_option("display.width", 200)

s, bank, bars = get()
s = s.sort_values(["dt", "underlying"]).reset_index(drop=True)
s["year"] = pd.to_datetime(s["dt"]).dt.year
s["fwd_ret"] = s["ret_3d"] * s["side"]

print("=" * 96)
print("0. REPRODUCTION")
print("=" * 96)
print(f"index rows {len(s)}  sessions {s['dt'].nunique()} "
      f"{s['dt'].min().date()}..{s['dt'].max().date()}")
print(pd.DataFrame({"banks": cohort_row(bank)}).T.to_string(float_format=lambda v: f"{v:8.3f}"))

# ------------------------------------------------------------------ NW t
def nw_t(x: pd.Series, lags: int = 10) -> float:
    x = pd.Series(x).dropna().astype(float)
    n = len(x)
    if n < 20: return np.nan
    e = x.values - x.mean()
    g0 = (e @ e) / n
    v = g0
    for L in range(1, min(lags, n - 1) + 1):
        gl = (e[L:] @ e[:-L]) / n
        v += 2 * (1 - L / (lags + 1)) * gl
    if v <= 0: return np.nan
    return float(x.mean() / np.sqrt(v / n))

def block_boot_p(x: pd.Series, block: int = 20, n_boot: int = 4000, seed: int = 7):
    """Circular block bootstrap of the mean under H0 (recentred)."""
    x = pd.Series(x).dropna().astype(float).values
    n = len(x)
    if n < 3 * block: return np.nan, np.nan
    obs = x.mean()
    c = x - obs                       # H0: mean 0, keeps the dependence
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=(n_boot, nb))
    offs = np.arange(block)
    idx = (starts[:, :, None] + offs[None, None, :]) % n
    means = c[idx.reshape(n_boot, -1)[:, :n]].mean(axis=1)
    p = float((np.abs(means) >= abs(obs)).mean())
    return obs, p

# ================================================================== A
print("\n" + "=" * 96)
print("A. LOOK-AHEAD IN THE DRAWDOWN BUCKET  -- the study's only significant")
print("   directional result. dd_t = close_t / cummax(close)_t - 1 uses the")
print("   SAME SESSION's close. The break is accepted at 10:15..15:15; the")
print("   close is not known then. The causal version lags it one session.")
print("=" * 96)
n = (s[s["underlying"] == "NIFTY"].sort_values("dt").set_index("dt")["close"].dropna())
dd_same = (n / n.cummax() - 1.0)
dd_prev = dd_same.shift(1)            # known at yesterday's close -> causal

s["dd_same"] = pd.to_datetime(s["dt"]).map(dd_same)
s["dd_prev"] = pd.to_datetime(s["dt"]).map(dd_prev)
print(f"   sessions with NO NIFTY row (dd is NaN): same {s['dd_same'].isna().sum()}"
      f"  prev {s['dd_prev'].isna().sum()}   <- BUG CHECK: np.where(NaN>=x) is False,")
print("   so any NaN session is silently swept into the 'drawdown' bucket.")

def bucketise(col, name):
    v = s[col]
    b = pd.Series(np.where(v >= -0.02, "at-highs",
                  np.where(v >= -0.07, "pullback", "drawdown")), index=s.index)
    b[v.isna()] = "NA"
    s[name] = b

bucketise("dd_same", "bkt_same")
bucketise("dd_prev", "bkt_prev")

for tag, col in (("SAME-SESSION close (as published)", "bkt_same"),
                 ("PRIOR-SESSION close (causal)", "bkt_prev")):
    print(f"\n   --- bucket from {tag} ---")
    rows = {}
    for k in ("at-highs", "pullback", "drawdown"):
        sub = s[s[col] == k]
        if not len(sub): continue
        r = cohort_row(sub)
        bi = per_session_break_bias(sub)
        m, t, nn = summarise(bi)
        rows[k] = {"name-rows": len(sub), "sessions": sub["dt"].nunique(),
                   "up%": r["up%"], "down%": r["down%"], "never%": r["never%"],
                   "d": m, "t": t, "NW t(10)": nw_t(bi),
                   "block-boot p": block_boot_p(bi)[1]}
    print(pd.DataFrame(rows).T.to_string(float_format=lambda v: f"{v:9.3f}"))
    for x, y in (("at-highs", "drawdown"), ("at-highs", "pullback")):
        a = per_session_break_bias(s[s[col] == x])
        b = per_session_break_bias(s[s[col] == y])
        d, t, n1, n2 = two_sample(a, b)
        print(f"     contrast {x} vs {y}: difference {d:+.3f}  Welch t {t:+.2f}  (n {n1}/{n2})")

# ================================================================== B
print("\n" + "=" * 96)
print("B. IS THE at-highs TILT ANYTHING BUT DRIFT?  The report kills the")
print("   ret_3d gap by pointing out the market drifts up. The SAME argument")
print("   applies to a break-RATE tilt: on an up-closing day the index breaks")
print("   up. 'at-highs' is defined off the close, so it SELECTS up days.")
print("=" * 96)
s["day_ret"] = s["close"] / s["ib_ref"] - 1.0      # IB close -> session close
nif = s[s["underlying"] == "NIFTY"].set_index("dt")
nif_ret = (nif["close"] / nif["close"].shift(1) - 1.0)
s["nifty_day"] = pd.to_datetime(s["dt"]).map(nif_ret)
rows = {}
for col, lbl in (("bkt_same", "same-session"), ("bkt_prev", "causal")):
    for k in ("at-highs", "pullback", "drawdown"):
        sub = s[s[col] == k]
        d = sub.drop_duplicates("dt")["nifty_day"]
        rows[(lbl, k)] = {"sessions": sub["dt"].nunique(),
                          "P(NIFTY day up)%": float((d > 0).mean() * PCT),
                          "mean NIFTY day ret pp": float(d.mean() * PCT),
                          "up%": cohort_row(sub)["up%"],
                          "down%": cohort_row(sub)["down%"]}
print(pd.DataFrame(rows).T.to_string(float_format=lambda v: f"{v:9.3f}"))

print("\n   CONTROL: within each bucket, condition on the SIGN of the session's")
print("   own move. If the tilt is only 'up days break up', it vanishes here.")
for col, lbl in (("bkt_same", "same-session"), ("bkt_prev", "causal")):
    print(f"   --- {lbl} ---")
    for k in ("at-highs", "pullback", "drawdown"):
        sub = s[(s[col] == k) & s["nifty_day"].notna()]
        for sg, nm in ((True, "NIFTY up day  "), (False, "NIFTY down day")):
            q = sub[(sub["nifty_day"] > 0) == sg]
            if not len(q): continue
            r = cohort_row(q); bi = per_session_break_bias(q)
            m, t, nn = summarise(bi)
            print(f"     {k:9s} {nm}  sess {q['dt'].nunique():4d}"
                  f"  up {r['up%']:5.2f}  down {r['down%']:5.2f}"
                  f"  d {m:+.3f} (t {t:+5.2f})")

print("\n   The unconditional index drift over the window, for reference:")
print(f"     NIFTY {n.iloc[0]:.0f} -> {n.iloc[-1]:.0f}"
      f"  = {(n.iloc[-1]/n.iloc[0]-1)*PCT:+.1f}% over {len(n)} sessions"
      f"  ({(n.iloc[-1]/n.iloc[0])**(252/len(n))*100-100:+.1f}%/yr)")
print(f"     P(NIFTY session up) over the whole window: "
      f"{float((nif_ret>0).mean()*PCT):.2f}%")
