"""MEASUREMENT PASS, stage 4: OPTION-LEVEL cells vs controls.

The decisive question (pre-registered): does the daily-regime filter lift the
OPTION-LEVEL outcome of each timer vs (C1) the same timer unfiltered and vs
(C2) random-inside-regime — net of costs, on the real deduped option tape
with modelled-when-missing exits?

Cost model (inherited verbatim from moves_rs/opt_selection.py — the tape has
no bid/ask, so spread is ASSUMED, and the full grid is reported):
    round-trip as fraction of premium: stock 8.0%, index 1.6%
    sensitivity grid: 0%, 2%, 5%, 10%

Option window: 2026-03-02 .. 2026-07-21 (the extracted tape; expiries
2026-03-31 .. 2026-08-25). ONE broadly-rising macro regime — caveat stands.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from controls import N_DRAWS, SEED
from spot_analyse import cell_entries, collapse_episodes, unfiltered_entries
from study_grid import (ALPHA, FDR_Q, HOLDS, MONEYNESS, PRIMARY_CELL, REGIMES,
                        TIMEFRAMES, TIMERS, grid_size)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
WINDOW_LO, WINDOW_HI = "2026-03-02", "2026-07-21"
COSTS = {"index": 0.016, "stock": 0.080}
COST_GRID = (0.0, 0.02, 0.05, 0.10)
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
               "SENSEX", "BANKEX"}
N_BOOT = 1000


def cost_of(und: pd.Series) -> np.ndarray:
    return np.where(und.isin(INDEX_NAMES), COSTS["index"], COSTS["stock"])


class EvalIndex:
    """(underlying, naive-time) -> option outcome rows, per (side, band)."""

    def __init__(self, ev: pd.DataFrame):
        self.sub = {}
        for (side, band), g in ev.groupby(["side", "band"], sort=False):
            self.sub[(side, band)] = g.set_index(["underlying", "time"])

    def attach(self, e: pd.DataFrame, band: str, hold: str) -> pd.DataFrame:
        """entries (with dir, naive time_n) -> + gross/net/modelled columns."""
        out = []
        for side, d in (("CE", 1), ("PE", -1)):
            part = e[e["dir"] == d]
            if part.empty:
                continue
            g = self.sub.get((side, band))
            if g is None:
                continue
            idx = pd.MultiIndex.from_arrays(
                [part["underlying"], part["time_n"]])
            got = g.reindex(idx)[
                [f"gross_{hold}", f"modelled_{hold}", f"method_{hold}",
                 "iv_present", "oi_present", "entry_px"]]
            got.columns = ["gross", "modelled", "method", "iv_p", "oi_p",
                           "entry_px"]
            out.append(pd.concat([part.reset_index(drop=True),
                                  got.reset_index(drop=True)], axis=1))
        if not out:
            return pd.DataFrame()
        r = pd.concat(out, ignore_index=True).dropna(subset=["gross"])
        r["net"] = r["gross"] - cost_of(r["underlying"])
        return r


def opt_stats(r: pd.DataFrame) -> dict:
    if len(r) == 0:
        return {"n": 0}
    s = r["net"].to_numpy(float)
    g = r["gross"].to_numpy(float)
    ex3 = np.sort(s)[:-3].mean() if len(s) > 3 else np.nan
    win = s > 0
    mdl = r["modelled"].to_numpy(bool)
    return {
        "n": len(s), "gross_mean": g.mean(), "mean": s.mean(),
        "median": float(np.median(s)), "hit": float(win.mean()),
        "ex_top3": float(ex3),
        "mdl_exit": float(mdl.mean()),
        "mdl_exit_win": float(mdl[win].mean()) if win.any() else np.nan,
        "mdl_exit_loss": float(mdl[~win].mean()) if (~win).any() else np.nan,
        "floor_rate": float((r["method"].to_numpy(float) == 2).mean()),
        **{f"net_at_{int(c * 100)}": float((g - c).mean()) for c in COST_GRID},
    }


def boot_sessions(r: pd.DataFrame, rng, col="net"):
    s = r[col].to_numpy(float)
    lab = r["session"].to_numpy()
    sess = pd.unique(lab)
    idx = {v: np.flatnonzero(lab == v) for v in sess}
    sums = np.array([s[idx[v]].sum() for v in sess])
    cnts = np.array([len(idx[v]) for v in sess], float)
    picks = rng.integers(0, len(sess), size=(N_BOOT, len(sess)))
    return sums[picks].sum(1) / np.maximum(cnts[picks].sum(1), 1)


def matched_option_null(e: pd.DataFrame, pools: dict, rng) -> np.ndarray:
    counts = e.groupby(["underlying", "dir"]).size()
    tot = np.zeros(N_DRAWS)
    cnt = np.zeros(N_DRAWS)
    for (und, d), k in counts.items():
        vals = pools.get((und, d))
        if vals is None or len(vals) == 0:
            continue
        pick = rng.integers(0, len(vals), size=(N_DRAWS, k))
        tot += vals[pick].sum(1)
        cnt += k
    with np.errstate(invalid="ignore"):
        return tot / np.maximum(cnt, 1)


def build_option_pools(univ: pd.DataFrame, evx: EvalIndex, regime: str | None,
                       band: str, hold: str) -> dict:
    pools = {}
    for d, side in ((1, "CE"), (-1, "PE")):
        sub = univ if regime is None else univ[univ[f"{regime}_lag1"] == d]
        g = evx.sub.get((side, band))
        if g is None:
            continue
        idx = pd.MultiIndex.from_arrays([sub["underlying"], sub["time_n"]])
        got = g.reindex(idx)[[f"gross_{hold}"]].reset_index(drop=True)
        got["underlying"] = sub["underlying"].to_numpy()
        got = got.dropna()
        got["net"] = (got[f"gross_{hold}"]
                      - cost_of(got["underlying"]))
        for und, gg in got.groupby("underlying"):
            pools[(und, d)] = gg["net"].to_numpy(float)
    return pools


def main() -> None:
    rng = np.random.default_rng(SEED)
    rows, monthly_rows = [], []
    for tf in TIMEFRAMES:
        univ = pd.read_parquet(os.path.join(DATA, f"univ_{tf}.parquet"))
        univ = univ[(univ["session"] >= WINDOW_LO)
                    & (univ["session"] <= WINDOW_HI)].copy()
        univ["time_n"] = (univ["time"].dt.tz_convert("UTC")
                          .dt.tz_localize(None))
        ev = pd.read_parquet(os.path.join(DATA, f"eval_{tf}.parquet"))
        evx = EvalIndex(ev)
        pool_memo: dict = {}

        def pools_for(regime, band, hold):
            k = (regime, band, hold)
            if k not in pool_memo:
                pool_memo[k] = build_option_pools(univ, evx, regime, band, hold)
            return pool_memo[k]

        for regime in REGIMES:
            for timer in TIMERS:
                e_all = cell_entries(univ, regime, timer)
                c1_all = unfiltered_entries(univ, timer)
                for hold in HOLDS:
                    e_h = collapse_episodes(e_all, hold)
                    c1_h = collapse_episodes(c1_all, hold)
                    for band in MONEYNESS:
                        pools_c2 = pools_for(regime, band, hold)
                        pools_c3 = pools_for(None, band, hold)
                        e = evx.attach(e_h, band, hold)
                        c1 = evx.attach(c1_h, band, hold)
                        st = opt_stats(e)
                        st1 = opt_stats(c1)
                        row = {"regime": regime, "timer": timer, "tf": tf,
                               "hold": hold, "band": band, **st,
                               **{f"c1_{k}": v for k, v in st1.items()
                                  if k in ("n", "mean", "gross_mean", "hit")}}
                        if st["n"] >= 30 and st1["n"] >= 30:
                            b_cell = boot_sessions(e, rng)
                            b_c1 = boot_sessions(c1, rng)
                            d_obs = st["mean"] - st1["mean"]
                            d_boot = b_cell - b_c1
                            row["d_c1"] = float(d_obs)
                            row["d_c1_p"] = float(
                                2 * min((d_boot - d_boot.mean() >= d_obs).mean(),
                                        (d_boot - d_boot.mean() <= d_obs).mean()))
                            null2 = matched_option_null(e, pools_c2, rng)
                            null3 = matched_option_null(e, pools_c3, rng)
                            row["c2_null_mean"] = float(null2.mean())
                            row["c2_pct"] = float((st["mean"] > null2).mean())
                            row["c2_z"] = float((st["mean"] - null2.mean())
                                                / max(null2.std(), 1e-12))
                            row["c3_null_mean"] = float(null3.mean())
                            row["beta_regime"] = float(null2.mean()
                                                       - null3.mean())
                            row["se_boot"] = float(b_cell.std())
                        rows.append(row)
                        if st["n"] >= 30:
                            e["month"] = e["session"].dt.to_period("M").astype(str)
                            for mo, gm in e.groupby("month"):
                                monthly_rows.append(
                                    {"regime": regime, "timer": timer,
                                     "tf": tf, "hold": hold, "band": band,
                                     "month": mo, "n": len(gm),
                                     "mean_net": float(gm["net"].mean()),
                                     "mean_gross": float(gm["gross"].mean())})
    res = pd.DataFrame(rows)
    res.to_parquet(os.path.join(DATA, "opt_cells.parquet"))
    mon = pd.DataFrame(monthly_rows)
    mon.to_parquet(os.path.join(DATA, "opt_monthly.parquet"))

    # ---------------- multiplicity across the pre-registered 256 tests ------
    gs = grid_size()
    ok = res.dropna(subset=["d_c1_p", "c2_z"]).copy()
    from scipy.stats import norm
    ok["p_c2"] = 1.0 - norm.cdf(ok["c2_z"])       # one-sided (better than null)
    pvals = np.concatenate([ok["d_c1_p"].to_numpy(), ok["p_c2"].to_numpy()])
    m = gs["total_tests"]
    bonf = gs["bonferroni_alpha"]
    n_bonf = int((pvals < bonf).sum())
    # BH-FDR at q=0.10 over all computed tests (missing cells count as failed)
    ps = np.sort(pvals)
    k = np.arange(1, len(ps) + 1)
    passed = ps <= k / m * FDR_Q
    thr = ps[passed].max() if passed.any() else 0.0
    print(f"grid: {gs}")
    print(f"computed tests: {len(pvals)} of {m}; bonferroni alpha {bonf:.3g}; "
          f"pass bonferroni: {n_bonf}; BH-FDR(q={FDR_Q}) threshold {thr:.4g}, "
          f"pass FDR: {int((pvals <= thr).sum()) if thr > 0 else 0}")

    prim = res[(res["regime"] == PRIMARY_CELL[0])
               & (res["timer"] == PRIMARY_CELL[1])
               & (res["tf"] == PRIMARY_CELL[2])
               & (res["hold"] == PRIMARY_CELL[3])
               & (res["band"] == PRIMARY_CELL[4])]
    print("\nPRIMARY PRE-REGISTERED CELL (alone at alpha=0.05):")
    print(prim.to_string(index=False))

    pd.set_option("display.width", 300)
    cols = ["regime", "timer", "tf", "hold", "band", "n", "gross_mean",
            "mean", "median", "hit", "ex_top3", "mdl_exit", "mdl_exit_win",
            "mdl_exit_loss", "c1_n", "c1_mean", "d_c1", "d_c1_p",
            "c2_null_mean", "c2_pct", "c2_z", "c3_null_mean", "beta_regime",
            "net_at_0", "net_at_2", "net_at_5", "net_at_10"]
    print("\nALL OPTION CELLS:")
    print(res[[c for c in cols if c in res.columns]].to_string(index=False))
    print("\nMONTHLY (non-overlapping periods), primary construct family "
          "(deep_macd 30m):")
    fam = mon[(mon["timer"] == "deep_macd") & (mon["tf"] == "30m")]
    print(fam.to_string(index=False))


if __name__ == "__main__":
    main()
