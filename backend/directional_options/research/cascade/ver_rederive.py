"""(VERIFY 7) Re-derive the REGIME premise and the CASCADE lift independently.

Neither headline is accepted from the shipped scripts:

  * the regime numbers are recomputed with an ADX written from scratch here
    (Wilder smoothing, no shared code with features.py), so an error in the
    shared indicator cannot reproduce itself;
  * the cascade probabilities are recomputed from the episode table with an
    independently written bootstrap, including the one framing the shipped
    pass says is decisive — P(large move) measured FROM THE STAGE-2 BAR, which
    is the only moment the second tranche could actually be bought.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import mat_run  # noqa: E402
import run_cascade as rc  # noqa: E402
import ver_stats as VS  # noqa: E402

DATA = os.path.join(HERE, "data")
OUT = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.append(s)


# ---------------------------------------------------------------- ADX, from scratch
def wilder_adx(h, l, c, n=14):
    """Wilder 1978 ADX, written independently of features.py."""
    h, l, c = map(lambda x: np.asarray(x, float), (h, l, c))
    m = len(c)
    tr = np.full(m, np.nan)
    pdm = np.zeros(m)
    ndm = np.zeros(m)
    for i in range(1, m):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
    atr = np.full(m, np.nan)
    sp = np.full(m, np.nan)
    sn = np.full(m, np.nan)
    if m <= n:
        return np.full(m, np.nan)
    atr[n] = np.nansum(tr[1:n + 1])
    sp[n] = pdm[1:n + 1].sum()
    sn[n] = ndm[1:n + 1].sum()
    for i in range(n + 1, m):
        atr[i] = atr[i - 1] - atr[i - 1] / n + tr[i]
        sp[i] = sp[i - 1] - sp[i - 1] / n + pdm[i]
        sn[i] = sn[i - 1] - sn[i - 1] / n + ndm[i]
    with np.errstate(invalid="ignore", divide="ignore"):
        pdi = 100 * sp / atr
        ndi = 100 * sn / atr
        dx = 100 * np.abs(pdi - ndi) / (pdi + ndi)
    adx = np.full(m, np.nan)
    k = n * 2
    if m > k:
        adx[k] = np.nanmean(dx[n + 1:k + 1])
        for i in range(k + 1, m):
            adx[i] = (adx[i - 1] * (n - 1) + dx[i]) / n
    return adx


def runs(mask):
    out, i = [], 0
    while i < len(mask):
        j = i
        while j < len(mask) and mask[j] == mask[i]:
            j += 1
        out.append((bool(mask[i]), j - i))
        i = j
    return out


def regime():
    p("=" * 78)
    p("[7a] REGIME PREMISE — re-derived with an independently written Wilder ADX")
    p("=" * 78)
    f = os.path.join(DATA, "regime", "daily.parquet")
    if not os.path.exists(f):
        p("  regime/daily.parquet missing — skipped")
        return
    d = pd.read_parquet(f)
    kcls = next((k for k in ("mkt", "mclass", "market") if k in d.columns), None)
    p(f"  rows {len(d)}  cols {list(d.columns)[:12]}")
    cl = {"c": None}
    for c_, h_, l_ in (("s_close", "s_high", "s_low"), ("close", "high", "low"),
                       ("c", "h", "l")):
        if cl["c"] is None and c_ in d.columns:
            cl["c"], cl["h"], cl["l"] = c_, h_, l_
    if cl["c"] is None:
        p("  unexpected schema — skipped")
        return
    rows = []
    key = "series_id" if "series_id" in d.columns else "underlying"
    for (u), g in d.groupby(key, sort=False):
        g = g.sort_values([c for c in ("sidx", "session") if c in g.columns][0])
        if len(g) < 150:
            continue
        a = wilder_adx(g[cl["h"]], g[cl["l"]], g[cl["c"]], 14)
        ok = np.isfinite(a)
        if ok.sum() < 100:
            continue
        for thr in (20, 25, 30):
            m = a[ok] >= thr
            rr = runs(m)
            tl = [n for t, n in rr if t]
            cn = [n for t, n in rr if not t]
            rows.append({"underlying": u,
                         "mkt": (g[kcls].iloc[0] if kcls else "?"),
                         "thr": thr, "share": float(m.mean()),
                         "med_trend": float(np.median(tl)) if tl else np.nan,
                         "med_chop": float(np.median(cn)) if cn else np.nan})
    r = pd.DataFrame(rows)
    p("\n  share of sessions TRENDING (ADX >= threshold), by market:")
    p(r.pivot_table(index="mkt", columns="thr", values="share",
                    aggfunc="mean").round(3).to_string())
    p("\n  median run length, sessions (trending / consolidating):")
    for thr in (20, 25, 30):
        s = r[r.thr == thr]
        p(f"    ADX>={thr}: trending {s['med_trend'].median():.1f}   "
          f"consolidating {s['med_chop'].median():.1f}   "
          f"n_series {len(s)}")
    s25 = r[r.thr == 25]["share"]
    p(f"\n  VERDICT P1 'consolidation dominates': at ADX>=25 mean trending share "
      f"= {s25.mean():.3f} -> non-trending {1-s25.mean():.3f}. "
      f"{'CONFIRMED' if s25.mean() < 0.5 else 'REJECTED'}")
    s20 = r[r.thr == 20]["share"]
    p(f"  THRESHOLD FRAGILITY (as the shipped pass reported): at ADX>=20 the "
      f"trending share is {s20.mean():.3f} -> the premise "
      f"{'INVERTS' if s20.mean() > 0.5 else 'still holds'}.")


def cascade():
    p("\n" + "=" * 78)
    p("[7b] CASCADE LIFT — re-derived from the episode builder, own bootstrap")
    p("=" * 78)
    intra, daily = rc.load()
    bars = rc.Bars(intra, daily)
    epi = mat_run.build_episodes(intra, daily, bars)
    p(f"  episodes {len(epi)}  families {sorted(epi.family.unique())}")

    # ---- P(stage-2 | stage-1) vs matched control
    p("\n  P(higher timeframe confirms within 3 sessions):")
    g = epi.groupby("family")["s2"].agg(["mean", "size"])
    p(g.round(4).to_string())
    a = epi[epi.family == "s1_primary"]
    ctrl = epi[epi.family.isin(["ctrl_long", "ctrl_short", "ctrl_random"])]
    dd, lo, hi, pv = VS.boot_diff(a["s2"].to_numpy(float), a["underlying"].to_numpy(),
                                  ctrl["s2"].to_numpy(float), ctrl["underlying"].to_numpy())
    p(f"    signal - control = {dd:+.4f}  95% CI [{lo:+.4f},{hi:+.4f}]  p={pv:.4f}"
      f"   ratio {a['s2'].mean()/ctrl['s2'].mean():.2f}x")

    # ---- P(large) at t1 and at t2
    rows = []
    for ep in epi.itertuples():
        B = bars.u.get(ep.underlying)
        if B is None:
            continue
        ps = rc.path_stats(B, ep.bar, int(ep.side), float(ep.atr_abs),
                           int(ep.s0) + rc.LARGE_HORIZON_SESSIONS)
        if not ps or ps["truncated"]:
            continue
        r = {"family": ep.family, "underlying": ep.underlying, "s2": int(ep.s2),
             "large_t1": ps["large"], "term_t1": ps["term_atr"], "large_t2": np.nan}
        if ep.s2:
            e2 = bars.first_bar.get((ep.underlying, int(ep.s2_sidx) + 1))
            if e2 is not None:
                a2 = float(B["atr"][e2])
                p2 = rc.path_stats(B, e2, int(ep.side), a2,
                                   int(B["sidx"][e2]) + rc.LARGE_HORIZON_SESSIONS)
                if p2 and not p2["truncated"]:
                    r["large_t2"] = p2["large"]
        rows.append(r)
    z = pd.DataFrame(rows)

    p("\n  P(sustained large move) — measured FROM THE STAGE-1 BAR "
      "(conditions on a LATER event, not decision-time information):")
    p(f"{'family':13s} {'n':>6s} {'all':>8s} {'n_s2':>6s} {'s2=1':>8s} "
      f"{'n_no':>6s} {'s2=0':>8s}")
    for fam in ("s1_primary", "ctrl_long", "ctrl_short", "ctrl_random"):
        w = z[z.family == fam]
        if w.empty:
            continue
        w1, w0 = w[w.s2 == 1], w[w.s2 == 0]
        p(f"{fam:13s} {len(w):6d} {w.large_t1.mean():8.4f} {len(w1):6d} "
          f"{w1.large_t1.mean():8.4f} {len(w0):6d} {w0.large_t1.mean():8.4f}")
    sig = z[(z.family == "s1_primary") & (z.s2 == 1)]
    con = z[(z.family.isin(["ctrl_long", "ctrl_short", "ctrl_random"])) & (z.s2 == 1)]
    dd, lo, hi, pv = VS.boot_diff(sig.large_t1.to_numpy(float), sig.underlying.to_numpy(),
                                  con.large_t1.to_numpy(float), con.underlying.to_numpy())
    p(f"    cascade vs CONTROL-bars-also-followed-by-a-confirm: {dd:+.4f} "
      f"[{lo:+.4f},{hi:+.4f}] p={pv:.4f}")
    p("    -> if this is ~0, the apparent cascade lift is the confirm being "
      "caused by the move, not predicting it.")

    p("\n  P(sustained large move) — measured FROM THE STAGE-2 BAR "
      "(the only tradeable moment for the second tranche):")
    for fam in ("s1_primary", "ctrl_long", "ctrl_short", "ctrl_random"):
        w = z[(z.family == fam) & z.large_t2.notna()]
        if len(w) < 20:
            continue
        m, lo, hi, pv = VS.boot_mean(w.large_t2.to_numpy(float),
                                     w.underlying.to_numpy())
        p(f"    {fam:13s} n={len(w):5d}  P(large)={m:.4f} [{lo:.4f},{hi:.4f}]")
    base = z.large_t1.mean()
    p(f"    unconditional base rate over ALL episodes = {base:.4f}")
    p(f"    break-even hit rate at the 2:1 barrier = {1/3:.4f} (before option carry)")


def main() -> None:
    regime()
    cascade()
    with open(os.path.join(HERE, "ver_rederive.txt"), "w") as fh:
        fh.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
