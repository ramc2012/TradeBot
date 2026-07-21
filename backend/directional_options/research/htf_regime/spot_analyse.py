"""MEASUREMENT PASS, stage 2: SPOT-LEVEL cells vs controls.

Question at this level: does (daily regime state) x (LTF timer) select bars
whose forward SPOT move (signed by regime direction) beats
  C1  the same timer with no regime filter,
  C2  random bars inside the same regime state (LOAD-BEARING - carries the
      full regime/market beta),
  C3  matched unconditional random bars (attributes C2-minus-C3 to the
      regime itself = mostly beta by hypothesis).

Statistic treatment (declared):
  - actual cell entries are collapsed to non-overlapping EPISODES per
    underlying (an entry inside the previous kept entry's hold window joins
    that episode and is dropped);
  - C2/C3 draws are matched to the COLLAPSED per-(underlying, direction)
    entry counts; drawn bars are not collapse-filtered (random bars are
    near-exchangeable; the comparison of MEANS is unbiased);
  - dispersion of the actual mean is session-block bootstrapped (1000 reps,
    resampling sessions with replacement) because same-session entries
    across names are cross-correlated;
  - C2/C3 give 200-draw nulls (seed 20260721); we report the percentile,
    and a normal-approximation z/p against the null (200 draws cannot
    resolve the Bonferroni threshold directly - the z-approx is declared).
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from controls import N_DRAWS, SEED
from study_grid import HOLDS, MONEYNESS, REGIMES, TIMEFRAMES, TIMERS, grid_size

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
N_BOOT = 1000
AGE_BUCKETS = ((1, 5), (6, 12), (13, 20), (21, 10_000))


# --------------------------------------------------------------- entry builds
def cell_entries(univ: pd.DataFrame, regime: str, timer: str) -> pd.DataFrame:
    up = univ[f"t_{timer}"] & (univ[f"{regime}_lag1"] == 1)
    dn = univ[f"t_{timer}_dn"] & (univ[f"{regime}_lag1"] == -1)
    e = univ[up | dn].copy()
    e["dir"] = np.where(univ.loc[e.index, f"t_{timer}"]
                        & (univ.loc[e.index, f"{regime}_lag1"] == 1), 1, -1)
    e["age"] = e[f"{regime}_age_lag1"]
    return e


def unfiltered_entries(univ: pd.DataFrame, timer: str) -> pd.DataFrame:
    up = univ[f"t_{timer}"]
    dn = univ[f"t_{timer}_dn"]
    e = univ[up | dn].copy()
    e["dir"] = np.where(univ.loc[e.index, f"t_{timer}"], 1, -1)
    return e


def collapse_episodes(e: pd.DataFrame, hold: str) -> pd.DataFrame:
    """Greedy per (underlying, dir): drop entries starting before the prior
    kept entry's exit timestamp."""
    if e.empty:
        return e
    e = e.dropna(subset=[f"ret_{hold}"]).sort_values(
        ["underlying", "dir", "time"], kind="mergesort")
    keep = np.zeros(len(e), dtype=bool)
    t = e["time"].to_numpy()
    x = e[f"exit_ts_{hold}"].to_numpy()
    grp = (e["underlying"] + e["dir"].astype(str)).to_numpy()
    last_exit = None
    last_grp = None
    for i in range(len(e)):
        if grp[i] != last_grp or t[i] >= last_exit:
            keep[i] = True
            last_exit = x[i]
            last_grp = grp[i]
    return e[keep]


# ------------------------------------------------------------------ stats kit
def _sig(e: pd.DataFrame, hold: str) -> np.ndarray:
    return (e["dir"].to_numpy(float) * e[f"ret_{hold}"].to_numpy(float))


def basic_stats(e: pd.DataFrame, hold: str) -> dict:
    s = _sig(e, hold)
    if len(s) == 0:
        return {"n": 0}
    ex3 = np.sort(s)[:-3].mean() if len(s) > 3 else np.nan
    return {"n": len(s), "mean": s.mean(), "median": float(np.median(s)),
            "hit": float((s > 0).mean()), "ex_top3": float(ex3)}


def session_boot_mean(e: pd.DataFrame, hold: str, rng) -> tuple[float, np.ndarray]:
    """Session-block bootstrap distribution of the mean signed return."""
    s = _sig(e, hold)
    lab = e["session"].to_numpy()
    sess = pd.unique(lab)
    idx = {v: np.flatnonzero(lab == v) for v in sess}
    sums = np.array([s[idx[v]].sum() for v in sess])
    cnts = np.array([len(idx[v]) for v in sess], float)
    picks = rng.integers(0, len(sess), size=(N_BOOT, len(sess)))
    return s.mean(), sums[picks].sum(1) / np.maximum(cnts[picks].sum(1), 1)


def matched_null(e: pd.DataFrame, pools: dict, hold: str,
                 rng) -> np.ndarray:
    """200 matched draws -> null distribution of the mean signed return.
    pools: {(underlying, dir): DataFrame w/ ret columns} for the universe."""
    counts = e.groupby(["underlying", "dir"]).size()
    tot = np.zeros(N_DRAWS)
    cnt = np.zeros(N_DRAWS)
    for (und, d), k in counts.items():
        pool = pools.get((und, d))
        if pool is None or len(pool) == 0:
            continue
        vals = d * pool[f"ret_{hold}"].to_numpy(float)
        vals = vals[~np.isnan(vals)]
        if len(vals) == 0:
            continue
        pick = rng.integers(0, len(vals), size=(N_DRAWS, k))
        tot += vals[pick].sum(1)
        cnt += k
    with np.errstate(invalid="ignore"):
        return tot / np.maximum(cnt, 1)


def build_pools(univ: pd.DataFrame, regime: str | None):
    """(underlying, dir) -> frame of eligible bars. regime=None => all bars
    (C3); else bars whose lag-1 state matches dir (C2)."""
    pools = {}
    cols = [f"ret_{h}" for h in HOLDS]
    for d in (1, -1):
        sub = univ if regime is None else univ[univ[f"{regime}_lag1"] == d]
        for und, g in sub.groupby("underlying"):
            pools[(und, d)] = g[cols]
    return pools


# ----------------------------------------------------------------------- main
def main() -> None:
    rng = np.random.default_rng(SEED)
    univs = {tf: pd.read_parquet(os.path.join(DATA, f"univ_{tf}.parquet"))
             for tf in TIMEFRAMES}
    rows = []
    age_rows = []
    for tf in TIMEFRAMES:
        u = univs[tf]
        pools_c3 = build_pools(u, None)
        for regime in REGIMES:
            pools_c2 = build_pools(u, regime)
            for timer in TIMERS:
                e_all = cell_entries(u, regime, timer)
                c1_all = unfiltered_entries(u, timer)
                for hold in HOLDS:
                    e = collapse_episodes(e_all, hold)
                    c1 = collapse_episodes(c1_all, hold)
                    st = basic_stats(e, hold)
                    st1 = basic_stats(c1, hold)
                    row = {"regime": regime, "timer": timer, "tf": tf,
                           "hold": hold,
                           **{k: v for k, v in st.items()},
                           **{f"c1_{k}": v for k, v in st1.items()}}
                    if st["n"] >= 30:
                        mean, boot = session_boot_mean(e, hold, rng)
                        row["se_boot"] = float(boot.std())
                        null2 = matched_null(e, pools_c2, hold, rng)
                        null3 = matched_null(e, pools_c3, hold, rng)
                        row["c2_null_mean"] = float(null2.mean())
                        row["c2_null_std"] = float(null2.std())
                        row["c2_pct"] = float((mean > null2).mean())
                        z = ((mean - null2.mean())
                             / max(null2.std(), 1e-12))
                        row["c2_z"] = float(z)
                        row["c3_null_mean"] = float(null3.mean())
                        row["beta_regime"] = float(null2.mean() - null3.mean())
                        # vs C1: session-block bootstrap of the difference
                        _, boot1 = session_boot_mean(c1, hold, rng)
                        d_obs = mean - st1["mean"]
                        d_boot = boot - boot1[:len(boot)]
                        row["d_c1"] = float(d_obs)
                        row["d_c1_p"] = float(
                            2 * min((d_boot - d_boot.mean() >= d_obs).mean(),
                                    (d_boot - d_boot.mean() <= d_obs).mean()))
                    rows.append(row)
                    # age buckets, primary hold only
                    if hold == "1d" and st["n"] >= 30:
                        for lo, hi in AGE_BUCKETS:
                            m = (e["age"] >= lo) & (e["age"] <= hi)
                            sa = basic_stats(e[m], hold)
                            age_rows.append({"regime": regime, "timer": timer,
                                             "tf": tf, "bucket": f"{lo}-{hi}",
                                             **sa})
    res = pd.DataFrame(rows)
    res.to_parquet(os.path.join(DATA, "spot_cells.parquet"))
    ages = pd.DataFrame(age_rows)
    ages.to_parquet(os.path.join(DATA, "spot_ages.parquet"))
    pd.set_option("display.width", 250)
    print(grid_size())
    print(res.to_string(index=False))
    print("\nAGE BUCKETS (hold=1d):")
    print(ages.to_string(index=False))


if __name__ == "__main__":
    main()
