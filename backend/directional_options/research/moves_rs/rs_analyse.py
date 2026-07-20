"""(B) Relative strength vs NIFTY — spot-level evidence.

Sections
  0. universe + coverage
  1. the RS formulations and how much they overlap
  2. IS RS JUST BETA?
  3. cross-sectional IC by horizon (raw t, de-overlapped t, Newey-West t)
  4. decile monotonicity
  5. era stability
  6. direction asymmetry (high-RS as the CE leg vs low-RS as the PE leg)
  7. does RS select instruments that MOVE more?
  8. multiplicity (Bonferroni + Benjamini-Hochberg) over the whole grid

Statistical care:
  * Every IC is CROSS-SECTIONAL (Spearman within one session across names),
    so a market-wide move cannot manufacture it.
  * Overlapping forward windows inflate the raw t badly. Every headline t is
    reported three ways: raw, de-overlapped (dates spaced h apart, averaged
    over all h phase offsets), and Newey-West(h-1). The DE-OVERLAPPED t is the
    one quoted in the verdict.
  * p-values are two-sided from the de-overlapped t.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rs_features import (  # noqa: E402
    BETA_WIN, HORIZONS, LOOKBACKS, add_xs_ranks, build_panel,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data_rs")
MIN_NAMES_PER_DATE = 40

FEATURES = (
    ["rs_ret_21", "rs_ret_63", "rs_slope_21", "alpha_21", "beta_120"]
)
PRIMARY = "rs_ret_21"


# ---------------------------------------------------------------- utilities
def xs_ic(panel: pd.DataFrame, feat: str, fwd: str) -> pd.Series:
    """Per-session Spearman IC across names."""
    sub = panel[["session", feat, fwd]].dropna()
    out = {}
    for s, g in sub.groupby("session"):
        if len(g) < MIN_NAMES_PER_DATE:
            continue
        r = stats.spearmanr(g[feat], g[fwd]).statistic
        if np.isfinite(r):
            out[s] = r
    return pd.Series(out).sort_index()


def nw_t(x: pd.Series, lag: int) -> float:
    x = x.dropna().to_numpy()
    n = len(x)
    if n < 10:
        return np.nan
    m = x.mean()
    e = x - m
    g0 = (e * e).sum() / n
    s = g0
    for L in range(1, lag + 1):
        gl = (e[L:] * e[:-L]).sum() / n
        s += 2.0 * (1.0 - L / (lag + 1.0)) * gl
    if s <= 0:
        return np.nan
    return m / np.sqrt(s / n)


def deoverlap_t(x: pd.Series, h: int):
    """Average the t-stat over all h non-overlapping phase offsets."""
    x = x.dropna()
    ts, ms, ns = [], [], []
    for off in range(h):
        sub = x.iloc[off::h]
        if len(sub) < 6:  # only ever binds inside the era split
            continue
        t = stats.ttest_1samp(sub, 0.0)
        ts.append(t.statistic)
        ms.append(sub.mean())
        ns.append(len(sub))
    if not ts:
        return np.nan, np.nan, np.nan, np.nan, 0
    tbar = float(np.mean(ts))
    nbar = float(np.mean(ns))
    p = 2 * (1 - stats.t.cdf(abs(tbar), df=max(nbar - 1, 1)))
    return float(np.mean(ms)), tbar, float(np.min(ts)), p, int(nbar)


def bh_fdr(pvals):
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    order = np.argsort(p)
    ranked = p[order]
    q = ranked * n / (np.arange(n) + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(q, 1.0)
    return out


def hdr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


# ------------------------------------------------------------------- panel
def load_panel() -> pd.DataFrame:
    cache = os.path.join(DATA, "rs_panel.parquet")
    if os.path.exists(cache):
        return pd.read_parquet(cache)
    daily = pd.read_parquet(os.path.join(DATA, "rs_daily.parquet"))
    panel = build_panel(daily, min_sessions=300)
    panel = add_xs_ranks(panel, FEATURES)
    panel.to_parquet(cache, index=False)
    return panel


def main() -> None:
    panel = load_panel()
    panel["session"] = pd.to_datetime(panel["session"])
    panel = panel.sort_values(["underlying", "session"]).reset_index(drop=True)
    # trailing realised vol -- causal, used only as an adversarial control
    panel["rv_21"] = panel.groupby("underlying")["r_s"].transform(
        lambda s: s.rolling(21).std())
    panel["absrs_21"] = panel["rs_ret_21"].abs()
    panel["absrs_norm"] = panel["absrs_21"] / panel["rv_21"].replace(0.0, np.nan)

    # -------------------------------------------------- 0. universe/coverage
    hdr("0. UNIVERSE AND COVERAGE")
    print(f"names            : {panel.underlying.nunique()}")
    print(f"sessions         : {panel.session.nunique()} "
          f"({panel.session.min().date()} .. {panel.session.max().date()})")
    print(f"name-days        : {len(panel):,}")
    cov = panel.groupby("underlying").size()
    print(f"sessions/name    : min {cov.min()} med {int(cov.median())} max {cov.max()}")
    print(f"months/name      : ~{cov.median()/21:.1f}")
    usable = panel.dropna(subset=[PRIMARY, "fwd_5"])
    print(f"rows usable for the primary feature at fwd_5: {len(usable):,} "
          f"over {usable.session.nunique()} sessions")
    print(f"first usable session (needs {BETA_WIN} sessions of beta): "
          f"{panel.dropna(subset=['beta_120']).session.min().date()}")
    print("\nNOTE: ~16 months of stock history is ONE regime. Nothing here is a")
    print("multi-cycle result and it must not be read as one.")

    # ------------------------------------------- 1. formulations and overlap
    hdr("1. RS FORMULATIONS AND HOW MUCH THEY OVERLAP")
    print("computed:")
    print("  rs_ret_21    log(P/N) 21-session relative return   [PRIMARY]")
    print("  rs_ret_63    same, 63 sessions")
    print("  rs_slope_21  normalised OLS slope of log(P/N), 21 sessions")
    print("  alpha_21     21-session return minus beta_120 x NIFTY return")
    print("  beta_120     the control")
    print("  *_rank       cross-sectional percentile of each, per session")
    print("\nPRIMARY = rs_ret_21. Reason: the lane chooses among instruments on a")
    print("single day, so the natural object is a same-day cross-sectional")
    print("comparison; 21 sessions matches the monthly frame the owner asked")
    print("about; and it is the plainest reading of 'relative strength vs NIFTY'.")
    print("\nIMPORTANT: a per-date Spearman IC is rank-invariant, so rs_ret_21 and")
    print("its cross-sectional percentile rs_ret_21_rank have IDENTICAL IC by")
    print("construction. The rank form is a distinct object only for decile and")
    print("pooled work, never for IC. Reporting them as two 'formulations' with")
    print("two ICs would be double counting, and is not done here.")
    print("\nmean per-session cross-sectional Spearman correlation between forms:")
    pairs = [("rs_ret_21", "rs_ret_63"), ("rs_ret_21", "rs_slope_21"),
             ("rs_ret_21", "alpha_21"), ("rs_ret_21", "beta_120"),
             ("alpha_21", "beta_120"), ("rs_slope_21", "alpha_21")]
    for a, b in pairs:
        s = panel[["session", a, b]].dropna()
        cs = s.groupby("session").apply(
            lambda g: stats.spearmanr(g[a], g[b]).statistic if len(g) >= MIN_NAMES_PER_DATE else np.nan,
            include_groups=False)
        print(f"  {a:12s} vs {b:12s}: {cs.mean():+.3f}")

    # ------------------------------------------------- 2. is RS just beta?
    hdr("2. IS RS JUST BETA? (the classic trap)")
    s = panel[["session", "rs_ret_21", "beta_120"]].dropna()
    cs = s.groupby("session").apply(
        lambda g: stats.spearmanr(g["rs_ret_21"], g["beta_120"]).statistic
        if len(g) >= MIN_NAMES_PER_DATE else np.nan, include_groups=False).dropna()
    print(f"(a) XS corr(RS_21, beta_120): mean {cs.mean():+.3f}  "
          f"sd {cs.std():.3f}  [{cs.min():+.2f} .. {cs.max():+.2f}]")

    print("\n(b) does the market's own direction drive the RS IC?")
    print("    NIFTY forward return sign vs the same-day RS IC")
    bench = panel.groupby("session")["bc"].first().sort_index()
    for h in HORIZONS:
        ic = xs_ic(panel, "rs_ret_21", f"fwd_{h}")
        nf = (bench.shift(-h) / bench - 1.0).reindex(ic.index)
        up, dn = ic[nf > 0], ic[nf <= 0]
        print(f"    h={h:2d}: IC | NIFTY up   {up.mean():+.4f} (n={len(up)})   "
              f"IC | NIFTY down {dn.mean():+.4f} (n={len(dn)})   "
              f"corr(IC, NIFTY fwd) {np.corrcoef(ic, nf)[0,1]:+.3f}")
    print("    If RS were pure beta, the IC would flip sign with the market and")
    print("    corr(IC, NIFTY fwd) would be strongly positive.")

    print("\n(c) beta as a standalone signal, and RS after beta is removed:")
    rows = []
    for h in HORIZONS:
        for f in ["rs_ret_21", "alpha_21", "beta_120"]:
            ic = xs_ic(panel, f, f"fwd_{h}")
            m, t, tmin, p, n = deoverlap_t(ic, h)
            rows.append((f, h, m, t, p))
            print(f"    {f:10s} h={h:2d}: IC {m:+.4f}  t_deoverlap {t:+.2f}  p {p:.3f}")
    print("    Verdict logic: if alpha_21 (beta-stripped) keeps whatever IC")
    print("    rs_ret_21 has, RS is NOT just beta. If beta_120 alone carries the")
    print("    same IC, it IS.")

    print("\n(d) Fama-MacBeth: fwd ~ z(RS_21) + z(beta_120), per session")
    for h in HORIZONS:
        sub = panel[["session", "rs_ret_21", "beta_120", f"fwd_{h}"]].dropna()
        b1, b2 = [], []
        for sess, g in sub.groupby("session"):
            if len(g) < MIN_NAMES_PER_DATE:
                continue
            X = np.column_stack([
                np.ones(len(g)),
                stats.zscore(g["rs_ret_21"].to_numpy()),
                stats.zscore(g["beta_120"].to_numpy()),
            ])
            y = g[f"fwd_{h}"].to_numpy()
            coef, *_ = np.linalg.lstsq(X, y, rcond=None)
            b1.append(coef[1]); b2.append(coef[2])
        b1, b2 = pd.Series(b1), pd.Series(b2)
        m1, t1, _, p1, _ = deoverlap_t(b1, h)
        m2, t2, _, p2, _ = deoverlap_t(b2, h)
        print(f"    h={h:2d}: RS  coef {m1*100:+.3f}%/sd  t {t1:+.2f}  p {p1:.3f}   |   "
              f"beta coef {m2*100:+.3f}%/sd  t {t2:+.2f}  p {p2:.3f}")

    print("\n(e) period-matched check: alpha_21 needs 120 sessions of beta burn-in,")
    print("    so it is measured on a LATER, SHORTER sample than rs_ret_21. Any")
    print("    apparent alpha advantage could be the period, not the feature.")
    print("    Re-running rs_ret_21 on alpha_21's exact date sample:")
    for h in HORIZONS:
        ic_a = xs_ic(panel, "alpha_21", f"fwd_{h}")
        ic_r = xs_ic(panel, "rs_ret_21", f"fwd_{h}").reindex(ic_a.index).dropna()
        ma, ta, _, pa, _ = deoverlap_t(ic_a, h)
        mr, tr, _, pr, _ = deoverlap_t(ic_r, h)
        print(f"    h={h:2d}: alpha_21 IC {ma:+.4f} (t {ta:+.2f})   "
              f"rs_ret_21 on SAME dates IC {mr:+.4f} (t {tr:+.2f})")

    # ---------------------------------------------------- 3. IC by horizon
    hdr("3. CROSS-SECTIONAL IC BY HORIZON")
    print(f"{'feature':12s} {'h':>3s} {'n_dates':>8s} {'meanIC':>8s} {'raw t':>7s} "
          f"{'deov t':>7s} {'worst':>7s} {'NW t':>7s} {'p_deov':>7s} {'IC>0%':>6s}")
    grid = []
    for f in FEATURES:
        for h in HORIZONS:
            ic = xs_ic(panel, f, f"fwd_{h}")
            raw_t = stats.ttest_1samp(ic, 0.0).statistic
            m, t, tmin, p, n = deoverlap_t(ic, h)
            print(f"{f:12s} {h:3d} {len(ic):8d} {ic.mean():+8.4f} {raw_t:+7.2f} "
                  f"{t:+7.2f} {tmin:+7.2f} {nw_t(ic, h-1):+7.2f} {p:7.3f} "
                  f"{(ic>0).mean()*100:5.1f}%")
            grid.append({"feature": f, "h": h, "ic": ic.mean(), "t": t, "p": p})
    grid = pd.DataFrame(grid)

    # ------------------------------------------------ 4. decile monotonicity
    hdr("4. DECILE MONOTONICITY (primary feature, XS-demeaned forward return)")
    for h in HORIZONS:
        sub = panel[["session", "underlying", PRIMARY, f"fwd_{h}"]].dropna().copy()
        sub = sub[sub.groupby("session")[PRIMARY].transform("size") >= MIN_NAMES_PER_DATE]
        sub["dec"] = sub.groupby("session")[PRIMARY].transform(
            lambda x: pd.qcut(x.rank(method="first"), 10, labels=False))
        sub["xs"] = sub[f"fwd_{h}"] - sub.groupby("session")[f"fwd_{h}"].transform("mean")
        dm = sub.groupby("dec")["xs"].mean() * 100
        rho = stats.spearmanr(dm.index, dm.values).statistic
        # top-minus-bottom spread as a per-date series -> de-overlapped t
        sp = sub[sub.dec == 9].groupby("session")["xs"].mean() - \
            sub[sub.dec == 0].groupby("session")["xs"].mean()
        m, t, tmin, p, n = deoverlap_t(sp.dropna(), h)
        print(f"h={h:2d}  " + "  ".join(f"D{i}:{dm[i]:+.2f}%" for i in range(10)))
        print(f"      monotonicity rho(decile, mean) = {rho:+.3f}   "
              f"D10-D1 {m*100:+.3f}%  t_deov {t:+.2f}  p {p:.3f}")

    # --------------------------------------------------- 5. era stability
    hdr("5. ERA STABILITY (primary feature; 4 equal-length eras)")
    dates = np.sort(panel.session.unique())
    edges = np.array_split(dates, 4)
    for h in HORIZONS:
        ic = xs_ic(panel, PRIMARY, f"fwd_{h}")
        line = [f"h={h:2d}:"]
        for i, e in enumerate(edges):
            seg = ic[(ic.index >= e[0]) & (ic.index <= e[-1])]
            if len(seg) < 10:
                line.append(f"E{i+1} n/a")
                continue
            m, t, _, p, _ = deoverlap_t(seg, h)
            line.append(f"E{i+1}[{pd.Timestamp(e[0]).date()}] IC {m:+.3f} t {t:+.2f}")
        print("   " + "  ".join(line))

    # ----------------------------------------------- 6. direction asymmetry
    hdr("6. DIRECTION ASYMMETRY — high-RS (CE leg) vs low-RS (PE leg)")
    print("raw = actual forward move (what an option would see, before costs).")
    print("xs  = the same, cross-sectionally demeaned (pure selection value).")
    for h in HORIZONS:
        sub = panel[["session", PRIMARY, f"fwd_{h}", f"fwd_hi_{h}", f"fwd_lo_{h}"]].dropna().copy()
        sub = sub[sub.groupby("session")[PRIMARY].transform("size") >= MIN_NAMES_PER_DATE]
        sub["dec"] = sub.groupby("session")[PRIMARY].transform(
            lambda x: pd.qcut(x.rank(method="first"), 10, labels=False))
        sub["xs"] = sub[f"fwd_{h}"] - sub.groupby("session")[f"fwd_{h}"].transform("mean")
        allm = sub[f"fwd_{h}"].mean() * 100
        for leg, dec, sign in (("CE / high-RS D10", 9, +1), ("PE / low-RS  D1", 0, -1)):
            g = sub[sub.dec == dec]
            raw = g[f"fwd_{h}"].mean() * 100 * sign
            xsm = g["xs"].mean() * 100 * sign
            win = ((g[f"fwd_{h}"] * sign) > 0).mean() * 100
            fav = (g[f"fwd_hi_{h}"] if sign > 0 else -g[f"fwd_lo_{h}"]).mean() * 100
            adv = (g[f"fwd_lo_{h}"] if sign > 0 else -g[f"fwd_hi_{h}"]).mean() * 100
            per = (g.groupby("session")["xs"].mean() * sign).dropna()
            m, t, _, p, _ = deoverlap_t(per, h)
            print(f"h={h:2d} {leg}: n {len(g):6d}  raw {raw:+.2f}%  xs {xsm:+.3f}% "
                  f"(t_deov {t:+.2f}, p {p:.3f})  win {win:.1f}%  "
                  f"MFE {fav:+.2f}%  MAE {adv:+.2f}%   [universe mean {allm:+.2f}%]")

    # --------------------------------------------- 7. does RS pick MOVERS?
    hdr("7. DOES RS SELECT INSTRUMENTS THAT MOVE MORE? (spot excursion)")
    print("fwd_exc_h = max(|max-high move|, |min-low move|) over t+1..t+h — the")
    print("excursion a long-premium trade needs in order to pay for theta.")
    panel["absrs_21"] = panel["rs_ret_21"].abs()
    for h in HORIZONS:
        for f in ["rs_ret_21", "absrs_21"]:
            ic = xs_ic(panel, f, f"fwd_exc_{h}")
            m, t, _, p, _ = deoverlap_t(ic, h)
            print(f"  h={h:2d} IC({f:10s} -> fwd excursion) {m:+.4f}  t_deov {t:+.2f}  p {p:.3f}")
    print("\n  ADVERSARIAL CONTROL: is |RS| just trailing volatility in disguise?")
    print("  rv_21 = trailing 21-session realised vol of daily returns (causal).")
    for h in HORIZONS:
        ic = xs_ic(panel, "rv_21", f"fwd_exc_{h}")
        m, t, _, p, _ = deoverlap_t(ic, h)
        print(f"  h={h:2d} IC(rv_21     -> fwd excursion) {m:+.4f}  t_deov {t:+.2f}  p {p:.3f}")
    for h in HORIZONS:
        ic = xs_ic(panel, "absrs_norm", f"fwd_exc_{h}")
        m, t, _, p, _ = deoverlap_t(ic, h)
        print(f"  h={h:2d} IC(|RS|/rv_21 -> fwd excursion) {m:+.4f}  t_deov {t:+.2f}  p {p:.3f}"
              "   <- |RS| with the vol component divided out")
    print("\n  Fama-MacBeth on ranks: fwd_exc ~ z(rank|RS|) + z(rank rv_21)")
    for h in HORIZONS:
        sub = panel[["session", "absrs_21", "rv_21", f"fwd_exc_{h}"]].dropna()
        b1, b2 = [], []
        for sess, g in sub.groupby("session"):
            if len(g) < MIN_NAMES_PER_DATE:
                continue
            X = np.column_stack([
                np.ones(len(g)),
                stats.zscore(g["absrs_21"].rank().to_numpy()),
                stats.zscore(g["rv_21"].rank().to_numpy()),
            ])
            coef, *_ = np.linalg.lstsq(X, g[f"fwd_exc_{h}"].to_numpy(), rcond=None)
            b1.append(coef[1]); b2.append(coef[2])
        m1, t1, _, p1, _ = deoverlap_t(pd.Series(b1), h)
        m2, t2, _, p2, _ = deoverlap_t(pd.Series(b2), h)
        print(f"  h={h:2d}: |RS| coef {m1*100:+.3f}%/sd t {t1:+.2f} p {p1:.3f}   |   "
              f"rv_21 coef {m2*100:+.3f}%/sd t {t2:+.2f} p {p2:.3f}")

    for h in (5,):
        sub = panel[["session", PRIMARY, f"fwd_exc_{h}"]].dropna().copy()
        sub = sub[sub.groupby("session")[PRIMARY].transform("size") >= MIN_NAMES_PER_DATE]
        sub["dec"] = sub.groupby("session")[PRIMARY].transform(
            lambda x: pd.qcut(x.rank(method="first"), 10, labels=False))
        dm = sub.groupby("dec")[f"fwd_exc_{h}"].mean() * 100
        print(f"  h={h} mean forward excursion by RS decile: " +
              "  ".join(f"D{i}:{dm[i]:.2f}%" for i in range(10)))

    # ----------------------------------------------------- 8. multiplicity
    hdr("8. MULTIPLICITY OVER THE FULL IC GRID")
    n = len(grid)
    grid = grid.sort_values("p").reset_index(drop=True)
    grid["p_bonf"] = np.minimum(grid["p"] * n, 1.0)
    grid["q_bh"] = bh_fdr(grid["p"].to_numpy())
    print(f"grid size = {n} tests ({len(FEATURES)} features x {len(HORIZONS)} horizons); "
          f"Bonferroni alpha = {0.05/n:.4f}")
    print(grid.to_string(index=False,
                         float_format=lambda v: f"{v:+.4f}" if abs(v) < 1 else f"{v:+.2f}"))
    surv = grid[grid.q_bh < 0.05]
    print(f"\nsurviving BH-FDR 5%: {len(surv)} / {n}")
    print(f"surviving Bonferroni : {int((grid.p_bonf < 0.05).sum())} / {n}")


if __name__ == "__main__":
    main()
