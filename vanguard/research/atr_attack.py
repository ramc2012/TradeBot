"""ADVERSARIAL RE-EXAMINATION of research/atr_refutation.py.

The report under attack claims (a) the atr20 look-ahead is real but costs only
0.011 IC, and (b) the finding dies anyway because normalised by volatility the
IC goes to -0.03, i.e. atr20 was an identity.

This script checks the checks:

  A  sample composition -- how many names per session, and when
  B  the t-stats: mfe_total spans t..t+3 so consecutive session ICs share
     outcome windows.  Newey-West and a moving-block bootstrap on the SAME
     session IC series.
  C  concentration by NAME, which the report never tested (it only drops
     sessions).  Leave-one-name-out and every leave-two-names-out pair.
  D  drop-2 in the CLAIM's direction.  ic_line always drops the 2 HIGHEST ICs,
     which for a negative claim is anti-conservative.
  E  CALIBRATION of the normalised test.  Under "it is a pure identity", what
     IC should atr20_lag get against mfe/atr_prior?  Feed the test an outcome
     that IS an identity (today's true range) and read off the null.
  F  atr_prior is divided by close.shift(21), a price 21 sessions stale.  Redo
     with prev_close and with a 60-session disjoint window.
  G  ERRORS-IN-VARIABLES.  atr20_lag is a noisy proxy for expected vol, so a
     sort on it exaggerates the true vol spread and any "payoff per unit vol"
     ratio is biased DOWN.  Pooled cross-sectional slope of log(mfe) on
     log(atr), OLS vs IV (instrument = disjoint ATR), tested against 1.
  H  cost.
  I  THE TEST THE REPORT SAYS IT DID NOT DO: price the actual ATM CE/PE at the
     break bar and ask whether the option return, and realised-over-implied,
     tilt with atr20_lag.  That is "already priced" measured, not assumed.
  J  the bonus ib_pct break-rate result against its own stated confound.

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/atr_attack.py
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.atr_refutation import add_clean_features, session_ics, t_of  # noqa: E402
from research.banknifty_rotation import BANKS  # noqa: E402
from research.mp_profile import FWD_SESSIONS, dsn, load  # noqa: E402
from research.mp_option_leg import build_trades, resolve  # noqa: E402

BANK_UNIVERSE = ("BANKNIFTY",) + BANKS
CACHE = "/tmp/atr_attack_sessions.pkl"
warnings.filterwarnings("ignore")
rng = np.random.default_rng(20260828)


# --------------------------------------------------------------- stats helpers
def nw_t(x: np.ndarray, lag: int) -> float:
    """t of the mean with a Newey-West HAC variance (Bartlett kernel)."""
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(x)
    if n < 10:
        return np.nan
    e = x - x.mean()
    g0 = (e @ e) / n
    v = g0
    for l in range(1, lag + 1):
        gl = (e[l:] @ e[:-l]) / n
        v += 2.0 * (1.0 - l / (lag + 1.0)) * gl
    if v <= 0:
        return np.nan
    return x.mean() / np.sqrt(v / n)


def block_boot_t(x: np.ndarray, block: int = 5, reps: int = 4000) -> tuple[float, float]:
    """Moving-block bootstrap: (t-equivalent, two-sided p) for mean(x)!=0."""
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(x)
    if n < 30:
        return np.nan, np.nan
    nb = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=(reps, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    means = x[idx.reshape(reps, -1)[:, :n]].mean(axis=1)
    centred = means - means.mean()
    se = centred.std(ddof=1)
    p = (np.abs(centred) >= abs(x.mean())).mean()
    return (x.mean() / se if se > 0 else np.nan), p


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    ra -= ra.mean()
    rb -= rb.mean()
    d = np.sqrt((ra @ ra) * (rb @ rb))
    return float(ra @ rb / d) if d > 0 else np.nan


def ics_matrix(d: pd.DataFrame, feature: str, target: str, min_names: int = 6):
    """Wide (session x name) matrices so name subsets can be dropped cheaply."""
    dd = d.dropna(subset=[feature, target])
    f = dd.pivot_table(index="dt", columns="underlying", values=feature)
    y = dd.pivot_table(index="dt", columns="underlying", values=target)
    y = y.reindex(columns=f.columns)
    return f, y, min_names


def ic_from_matrix(f: pd.DataFrame, y: pd.DataFrame, keep, min_names: int) -> np.ndarray:
    fa = f[keep].to_numpy()
    ya = y[keep].to_numpy()
    out = []
    for i in range(fa.shape[0]):
        m = np.isfinite(fa[i]) & np.isfinite(ya[i])
        if m.sum() < min_names:
            continue
        r = _spearman(fa[i][m], ya[i][m])
        if np.isfinite(r):
            out.append(r)
    return np.asarray(out)


def line(label: str, ic: np.ndarray) -> str:
    if len(ic) < 30:
        return f"   {label:<34}{'too few sessions':>40}"
    tb, pb = block_boot_t(ic)
    return (f"   {label:<34}{ic.mean():>+8.3f}{t_of(ic):>+8.2f}"
            f"{nw_t(ic, 4):>+9.2f}{tb:>+9.2f}{pb:>8.3f}{len(ic):>7}")


def hdr() -> str:
    return (f"   {'feature / target':<34}{'IC':>8}{'t(iid)':>8}"
            f"{'t(NW4)':>9}{'t(boot)':>9}{'p(boot)':>8}{'sess':>7}")


# ------------------------------------------------------------------------ data
def get_sessions(args) -> pd.DataFrame:
    if os.path.exists(CACHE) and not args.refresh:
        return pd.read_pickle(CACHE)
    start = date.today() - timedelta(days=args.lookback_days)
    conn = psycopg2.connect(args.dsn)
    try:
        s = load(conn, list(BANK_UNIVERSE), start)
    finally:
        conn.close()
    s = add_clean_features(s)
    s.to_pickle(CACHE)
    return s


def extra_features(s: pd.DataFrame) -> pd.DataFrame:
    """Alternative disjoint normalisers, all strictly prior-data-only."""
    out = []
    for _, g in s.groupby("underlying", sort=False):
        g = g.sort_values("dt").reset_index(drop=True)
        prev_close = g["close"].shift(1)
        tr = pd.concat([g["high"] - g["low"],
                        (g["high"] - prev_close).abs(),
                        (g["low"] - prev_close).abs()], axis=1).max(axis=1)
        # same t-40..t-21 window, but scaled by YESTERDAY's price, not a
        # 21-session-stale one (the report's version carries 21 days of drift)
        g["atr_prior_pc"] = tr.shift(21).rolling(20, min_periods=10).mean() / prev_close
        # a longer, quieter disjoint window: t-80..t-21
        g["atr_prior60"] = tr.shift(21).rolling(60, min_periods=30).mean() / prev_close
        # the report's own version, re-derived for the record
        g["atr_prior_rep"] = (tr.shift(21).rolling(20, min_periods=10).mean()
                              / g["close"].shift(21))
        # 21-session drift, the thing the stale divisor smuggles in
        g["drift21"] = prev_close / g["close"].shift(21) - 1.0
        out.append(g)
    return pd.concat(out, ignore_index=True)


# ----------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=700)
    ap.add_argument("--dsn", default=dsn())
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--skip-options", action="store_true")
    args = ap.parse_args()

    s = get_sessions(args)
    s = extra_features(s)

    b = s[s["side"] != 0].dropna(subset=["atr20_leak", "atr20_lag", "atr_prior"]).copy()
    b["mfe_norm"] = b["mfe_total"] / b["atr20_lag"]
    b["mfe_norm_disjoint"] = b["mfe_total"] / b["atr_prior"]
    b["mfe_norm_pc"] = b["mfe_total"] / b["atr_prior_pc"]
    b["mfe_norm_60"] = b["mfe_total"] / b["atr_prior60"]
    b["range_pct"] = (b["high"] - b["low"]) / b["close"]
    b["tr_pct"] = b["tr_today"]
    # PURE-IDENTITY placebo outcomes: quantities that ARE today's volatility
    b["ident_range_norm"] = b["range_pct"] / b["atr_prior"]
    b["ident_tr_norm"] = b["tr_pct"] / b["atr_prior"]
    b["ident_ib_norm"] = b["ib_width"] / b["atr_prior"]

    print(f"sample: {len(b):,} breaks, {b['dt'].nunique()} sessions, "
          f"{b['underlying'].nunique()} names, "
          f"{b['dt'].min().date()}..{b['dt'].max().date()}")

    # ---------------------------------------------------------------- A
    print("\n=== A. SAMPLE COMPOSITION (session IC needs >=6 names) ===")
    per = b.groupby("dt").size()
    used = per[per >= 6]
    print(f"   sessions with >=6 breaking names: {len(used)} of {len(per)}")
    print(f"   first used session {used.index.min().date()}, "
          f"last {used.index.max().date()}")
    yr = b.groupby(b["dt"].dt.to_period("Q")).agg(
        names=("underlying", "nunique"), rows=("underlying", "size"))
    print("   by quarter: " + "  ".join(f"{str(k)}:{v.rows}/{v.names}n"
                                        for k, v in yr.iterrows()))
    ics_all = session_ics(b, "atr20_lag", "mfe_total")
    h = len(ics_all) // 2
    print(f"   split-half boundary session: {ics_all['dt'].iloc[h].date()}   "
          f"(first half {ics_all['dt'].iloc[0].date()}..{ics_all['dt'].iloc[h-1].date()})")

    # ---------------------------------------------------------------- B
    print("\n=== B. OVERLAPPING OUTCOME WINDOWS: HAC AND BLOCK-BOOTSTRAP t ===")
    print("   mfe_total spans sessions t..t+3, so session ICs at t, t+1, t+2 share")
    print("   outcome bars. The report's t assumes independent sessions.")
    print(hdr())
    for tgt in ("mfe_total", "mfe_norm", "mfe_norm_disjoint", f"mfe_{FWD_SESSIONS}d",
                "mfe3d_norm" if "mfe3d_norm" in b else "mfe_total"):
        if tgt not in b.columns:
            b["mfe3d_norm"] = b[f"mfe_{FWD_SESSIONS}d"] / b["atr20_lag"]
    b["mfe3d_norm"] = b[f"mfe_{FWD_SESSIONS}d"] / b["atr20_lag"]
    for tgt in ("mfe_total", "mfe_norm", "mfe_norm_disjoint", "mfe3d_norm"):
        ic = session_ics(b, "atr20_lag", tgt)["ic"].to_numpy()
        print(line(f"atr20_lag -> {tgt}", ic))
    ic = session_ics(b, "atr20_leak", "mfe_total")["ic"].to_numpy()
    print(line("atr20_leak -> mfe_total", ic))

    # ---------------------------------------------------------------- C
    print("\n=== C. CONCENTRATION BY NAME (the report only ever drops sessions) ===")
    for tgt, direction in (("mfe_total", +1), ("mfe_norm_disjoint", -1)):
        f, y, mn = ics_matrix(b, "atr20_lag", tgt)
        names = list(f.columns)
        full = ic_from_matrix(f, y, names, mn)
        print(f"\n   target={tgt}   full IC {full.mean():+.3f} (t {t_of(full):+.2f}, "
              f"{len(full)} sessions)")
        loo = []
        for nm in names:
            keep = [x for x in names if x != nm]
            v = ic_from_matrix(f, y, keep, mn)
            loo.append((nm, v.mean(), t_of(v)))
        loo.sort(key=lambda r: r[1] * direction)
        print("      most-load-bearing names (removal moves IC against the claim):")
        for nm, m, tt in loo[:4]:
            print(f"        drop {nm:<14} IC {m:+.3f}  t {tt:+.2f}")
        best2, worst = None, None
        for a, c in itertools.combinations(names, 2):
            keep = [x for x in names if x not in (a, c)]
            v = ic_from_matrix(f, y, keep, mn)
            sc = v.mean() * direction
            if worst is None or sc < worst:
                worst, best2 = sc, (a, c, v.mean(), t_of(v), len(v))
        a, c, m, tt, nn = best2
        print(f"      WORST leave-two-names-out ({a}, {c}): IC {m:+.3f}  t {tt:+.2f}")
        # BANKNIFTY alone vs stocks alone
        st = [x for x in names if x != "BANKNIFTY"]
        v = ic_from_matrix(f, y, st, mn)
        print(f"      stocks only (no BANKNIFTY): IC {v.mean():+.3f}  t {t_of(v):+.2f}")

    # ---------------------------------------------------------------- D
    print("\n=== D. DROP-2 IN THE CLAIM'S OWN DIRECTION ===")
    print("   ic_line() always drops the 2 HIGHEST session ICs. For a claim that an")
    print("   IC is NEGATIVE that removes the two most inconvenient sessions.")
    print(f"   {'target':<26}{'IC':>8}{'drop2 hi':>10}{'drop2 lo':>10}"
          f"{'t drop2 lo':>12}{'drop5 lo':>10}")
    for tgt in ("mfe_norm", "mfe_norm_disjoint", "mfe3d_norm"):
        ic = np.sort(session_ics(b, "atr20_lag", tgt)["ic"].to_numpy())
        print(f"   {tgt:<26}{ic.mean():>+8.3f}{ic[:-2].mean():>+10.3f}"
              f"{ic[2:].mean():>+10.3f}{t_of(ic[2:]):>+12.2f}{ic[5:].mean():>+10.3f}")

    # ---------------------------------------------------------------- E
    print("\n=== E. CALIBRATION: WHAT IS THE NULL FOR THE NORMALISED TEST? ===")
    print("   If atr20 really is 'an identity', an outcome that IS today's volatility")
    print("   should score the same on mfe/atr_prior as mfe_total does. Read off the")
    print("   null by feeding the test genuine identities.")
    print(hdr())
    for tgt, lab in (("ident_tr_norm", "TRUE RANGE today / atr_prior  (pure identity)"),
                     ("ident_range_norm", "session range / atr_prior     (pure identity)"),
                     ("ident_ib_norm", "IB width / atr_prior          (pure identity)"),
                     ("mfe_norm_disjoint", "mfe_total / atr_prior         (the claim)")):
        ic = session_ics(b, "atr20_lag", tgt)["ic"].to_numpy()
        print(line(lab, ic))

    # ---------------------------------------------------------------- F
    print("\n=== F. THE DISJOINT NORMALISER'S STALE DIVISOR ===")
    print("   atr_prior = tr[t-40..t-21].mean() / close[t-21]. The divisor is a price")
    print("   21 sessions old, so 21 sessions of drift ride in the denominator.")
    d = b.dropna(subset=["drift21"])
    print(f"   |21-session drift| median {d['drift21'].abs().median()*100:.2f}%  "
          f"p90 {d['drift21'].abs().quantile(.9)*100:.2f}%   "
          f"spearman(drift21, atr20_lag) = "
          f"{d['drift21'].corr(d['atr20_lag'], method='spearman'):+.3f}")
    print(hdr())
    for tgt, lab in (("mfe_norm_disjoint", "/ atr_prior      (report, close[t-21])"),
                     ("mfe_norm_pc", "/ atr_prior_pc   (same window, close[t-1])"),
                     ("mfe_norm_60", "/ atr_prior60    (t-80..t-21, close[t-1])")):
        ic = session_ics(b, "atr20_lag", tgt)["ic"].to_numpy()
        print(line(lab, ic))

    # ---------------------------------------------------------------- G
    print("\n=== G. ERRORS-IN-VARIABLES: IS THE PAYOFF REALLY SUB-PROPORTIONAL? ===")
    print("   A sort on atr20_lag overstates the true vol spread (noise), so any")
    print("   payoff/ATR ratio falls with ATR even when payoff is exactly")
    print("   proportional. Pooled within-session slope of log(mfe) on log(atr),")
    print("   OLS (attenuated) and IV with the disjoint ATR as instrument.")
    g = b.dropna(subset=["mfe_total", "atr20_lag", "atr_prior_pc"]).copy()
    g = g[g["mfe_total"] > 0]
    g["ly"] = np.log(g["mfe_total"])
    g["lx"] = np.log(g["atr20_lag"])
    g["lz"] = np.log(g["atr_prior_pc"])
    for c in ("ly", "lx", "lz"):
        g[c] = g[c] - g.groupby("dt")[c].transform("mean")

    def slopes(df):
        x, y, z = df["lx"].to_numpy(), df["ly"].to_numpy(), df["lz"].to_numpy()
        ols = (x @ y) / (x @ x)
        iv = (z @ y) / (z @ x)
        return ols, iv

    ols, iv = slopes(g)
    sess = g["dt"].unique()
    bs = []
    for _ in range(1000):
        pick = rng.choice(sess, size=len(sess), replace=True)
        dd = pd.concat([g[g["dt"] == p] for p in pd.unique(pick)]) if False else \
            g.set_index("dt").loc[pick].reset_index()
        bs.append(slopes(dd))
    bs = np.array(bs)
    print(f"   n={len(g):,} breaks with mfe>0 ({(b['mfe_total'] <= 0).mean()*100:.1f}% "
          f"of breaks had mfe_total<=0 and are dropped)")
    print(f"   OLS slope {ols:+.3f}  [95% {np.percentile(bs[:,0],2.5):+.3f},"
          f" {np.percentile(bs[:,0],97.5):+.3f}]   "
          f"t vs 1 = {(ols-1)/bs[:,0].std(ddof=1):+.2f}")
    print(f"   IV  slope {iv:+.3f}  [95% {np.percentile(bs[:,1],2.5):+.3f},"
          f" {np.percentile(bs[:,1],97.5):+.3f}]   "
          f"t vs 1 = {(iv-1)/bs[:,1].std(ddof=1):+.2f}")
    print("   slope=1 means payoff scales one-for-one with volatility (nothing gained,")
    print("   nothing lost). slope<1 is the report's 'flat-to-declining' claim.")
    # the same slope for a pure identity, as a sanity floor
    gi = b.dropna(subset=["range_pct", "atr20_lag", "atr_prior_pc"]).copy()
    gi = gi[gi["range_pct"] > 0]
    gi["ly"] = np.log(gi["range_pct"])
    gi["lx"] = np.log(gi["atr20_lag"])
    gi["lz"] = np.log(gi["atr_prior_pc"])
    for c in ("ly", "lx", "lz"):
        gi[c] = gi[c] - gi.groupby("dt")[c].transform("mean")
    o2, i2 = slopes(gi)
    print(f"   CONTROL, outcome = today's realised range (a true identity):"
          f"  OLS {o2:+.3f}   IV {i2:+.3f}")

    # ---------------------------------------------------------------- H
    print("\n=== H. COST (0.05% per side on spot) ===")
    for c in (0.0, 0.0010, 0.0020):
        bb = b.copy()
        bb["net"] = bb["mfe_total"] - c
        bb["net_n"] = bb["net"] / bb["atr_prior_pc"]
        ic1 = session_ics(bb, "atr20_lag", "net")["ic"].to_numpy()
        ic2 = session_ics(bb, "atr20_lag", "net_n")["ic"].to_numpy()
        q = bb.dropna(subset=["atr20_lag"]).copy()
        parts = []
        for _, gg in q.groupby("dt"):
            if len(gg) < 10:
                continue
            gg = gg.copy()
            gg["q"] = pd.qcut(gg["atr20_lag"].rank(method="first"), 5, labels=False)
            parts.append(gg)
        qq = pd.concat(parts)
        med = qq.groupby("q")["net"].median() * 100
        print(f"   round-trip {c*100:.2f}%:  IC(raw) {ic1.mean():+.3f} "
              f"t {t_of(ic1):+.2f} | IC(/atr_prior_pc) {ic2.mean():+.3f} "
              f"t {t_of(ic2):+.2f} | median net MFE by ATR quintile "
              + " ".join(f"{v:.2f}%" for v in med))

    # ---------------------------------------------------------------- J
    print("\n=== J. THE ib_pct BREAK-RATE BONUS, AGAINST ITS OWN CONFOUND ===")
    fs = s.dropna(subset=["ib_pct", "atr20_lag"]).copy()
    fs["broke"] = (fs["side"] != 0).astype(float)
    ic = session_ics(fs, "ib_pct", "broke")["ic"].to_numpy()
    print(hdr())
    print(line("ib_pct -> broke", ic))
    ic = session_ics(fs, "ib_width", "broke")["ic"].to_numpy()
    print(line("ib_width -> broke", ic))
    # is ib_pct anything beyond ib_width? residual rank of ib_pct on ib_width
    r = fs.dropna(subset=["ib_pct", "ib_width"]).copy()
    r["rp"] = r.groupby("dt")["ib_pct"].rank(pct=True)
    r["rw"] = r.groupby("dt")["ib_width"].rank(pct=True)
    cov = r[["rp", "rw"]].cov()
    beta = cov.loc["rp", "rw"] / cov.loc["rw", "rw"]
    r["ib_pct_resid"] = r["rp"] - beta * r["rw"]
    ic = session_ics(r, "ib_pct_resid", "broke")["ic"].to_numpy()
    print(line("ib_pct orthogonalised to ib_width", ic))
    bk = s[s["side"] != 0].dropna(subset=["ib_pct", "break_frac"])
    print(f"   spearman(ib_pct, break_frac | breaks) = "
          f"{bk['ib_pct'].corr(bk['break_frac'], method='spearman'):+.3f}   "
          f"(narrow box -> earlier break, the stated confound)")

    if args.skip_options:
        return 0

    # ---------------------------------------------------------------- I
    print("\n=== I. THE TEST THE REPORT DID NOT DO: PRICE THE ACTUAL OPTION ===")
    ev = s[(s["side"] != 0) & s["break_ts"].notna()].copy()
    conn = psycopg2.connect(args.dsn)
    try:
        con, exits = resolve(conn, ev)
    finally:
        conn.rollback()
        conn.close()
    t = build_trades(con, exits)
    t = t.merge(ev[["underlying", "dt", "mfe_total", f"ret_{FWD_SESSIONS}d",
                    "atr20_lag", "atr_prior_pc", "ib_width"]],
                on=["underlying", "dt"], how="left")
    t["traded"] = np.where(t["side"] == 1, t["option_type"] == "CE",
                           t["option_type"] == "PE")
    t["prem_pct"] = t["prem"] / t["entry_spot"]
    for hh in (0, 3):
        t[f"r{hh}"] = t[f"exit_{hh}"] / t["prem"] - 1.0 - 0.02
    tr = t[t["traded"]].dropna(subset=["atr20_lag"]).copy()
    print(f"   resolved traded legs: {len(tr):,} over "
          f"{tr['dt'].nunique()} sessions, {tr['dt'].min().date()}..{tr['dt'].max().date()}")

    # does premium actually scale with atr20?  the report's whole assumption
    print("\n   I-1. IS VOLATILITY ACTUALLY PRICED IN THE PREMIUM?")
    iv_ = pd.to_numeric(tr["iv"], errors="coerce")
    if iv_.median() > 1.5:
        iv_ = iv_ / 100.0
    tr["ivf"] = iv_
    print(f"      spearman(atr20_lag, premium/spot) = "
          f"{tr['atr20_lag'].corr(tr['prem_pct'], method='spearman'):+.3f}")
    print(f"      spearman(atr20_lag, iv)           = "
          f"{tr['atr20_lag'].corr(tr['ivf'], method='spearman'):+.3f}")
    print("      (the report ASSUMED premium is proportional to trailing ATR;"
          " these say how true that is)")

    parts = []
    for _, gg in tr.groupby("dt"):
        if len(gg) < 8:
            continue
        gg = gg.copy()
        gg["q"] = pd.qcut(gg["atr20_lag"].rank(method="first"), 5, labels=False,
                          duplicates="drop")
        parts.append(gg)
    qq = pd.concat(parts)
    print("\n   I-2. OPTION OUTCOME BY WITHIN-SESSION atr20_lag QUINTILE"
          "  (2% round-trip cost)")
    print(f"      {'q':<4}{'n':>7}{'atr20':>9}{'prem/spot':>11}{'iv':>8}"
          f"{'medMFE%':>10}{'MFE/iv3d':>10}{'meanR0%':>10}{'medR0%':>9}"
          f"{'meanR3%':>10}{'medR3%':>9}")
    qq["imp3"] = qq["ivf"] * np.sqrt(3 / 252.0)
    for q, gg in qq.groupby("q"):
        print(f"      {int(q)+1:<4}{len(gg):>7}{gg['atr20_lag'].median()*100:>8.2f}%"
              f"{gg['prem_pct'].median()*100:>10.2f}%{gg['ivf'].median()*100:>7.1f}%"
              f"{gg['mfe_total'].median()*100:>9.2f}%"
              f"{(gg['mfe_total']/gg['imp3']).median():>10.2f}"
              f"{gg['r0'].mean()*100:>+10.1f}{gg['r0'].median()*100:>+9.1f}"
              f"{gg['r3'].mean()*100:>+10.1f}{gg['r3'].median()*100:>+9.1f}")

    print("\n   I-3. SESSION IC OF atr20_lag AGAINST THE OPTION RESULT")
    print(hdr())
    qq["mfe_over_imp"] = qq["mfe_total"] / qq["imp3"]
    for tgt in ("r0", "r3", "mfe_over_imp"):
        ic = session_ics(qq, "atr20_lag", tgt, min_names=8)["ic"].to_numpy()
        print(line(f"atr20_lag -> {tgt}", ic))
    return 0


if __name__ == "__main__":
    sys.exit(main())
