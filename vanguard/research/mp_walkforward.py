"""Anchored walk-forward: choose the rule in-sample, score it only out-of-sample.

WHY THIS MODULE EXISTS. Every result in this project so far was measured on the
same data used to find it. Roughly thirty-five rule variants have been tried;
the best of thirty-five nulls clears t=2.5 routinely, which is why several
headline numbers collapsed once difference-tested. Walk-forward is the only
honest answer: pick the rule using data the test never sees, then concatenate the
untouched out-of-sample stretches into one curve.

THE PROTOCOL
    TRAIN_M months of history choose the best candidate rule by mean return
    the NEXT TEST_M months are traded with that choice and recorded
    the window then rolls forward by TEST_M and the choice is made again
    only the recorded out-of-sample stretches form the equity curve

Anchored (expanding) training is the default because a profile edge, if it
exists, should be slow-moving; a rolling window is available for comparison.

WHAT THIS DELIBERATELY DOES NOT DO. It does not tune thresholds inside the fold,
because a candidate set of a dozen named rules is already a selection and adding
a continuous parameter search on top would reintroduce exactly the overfitting
this is meant to measure. The candidate set is fixed up front and stated.

REPORTED HONESTLY: how often the selected rule CHANGES between folds. A strategy
whose winner reshuffles every fold has not found anything -- it is sampling
noise, and the out-of-sample curve will show it even when each in-sample fold
looked excellent.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def walk_forward(frame: pd.DataFrame, candidates: dict, ret_col: str,
                 train_m: int = 18, test_m: int = 6, anchored: bool = True,
                 min_trades: int = 12) -> dict:
    """candidates: {name: boolean Series aligned to `frame`}; frame needs `dt`."""
    f = frame.sort_values("dt").reset_index(drop=True)
    months = f["dt"].dt.to_period("M")
    uniq = sorted(months.unique())
    if len(uniq) < train_m + test_m:
        return {"error": f"need {train_m + test_m} months, have {len(uniq)}"}

    picks, oos, fold_rows = [], [], []
    start_i = 0
    for i in range(train_m, len(uniq), test_m):
        tr_months = uniq[start_i:i] if not anchored else uniq[0:i]
        te_months = uniq[i:i + test_m]
        if not len(te_months):
            break
        tr = f[months.isin(tr_months)]
        te = f[months.isin(te_months)]
        best, best_mu = None, -np.inf
        for name, mask in candidates.items():
            m = mask.reindex(tr.index).fillna(False)
            r = tr.loc[m, ret_col].dropna()
            if len(r) < min_trades:
                continue
            if r.mean() > best_mu:
                best, best_mu = name, r.mean()
        if best is None:
            continue
        te_mask = candidates[best].reindex(te.index).fillna(False)
        te_r = te.loc[te_mask, [ret_col, "dt"]].dropna()
        picks.append(best)
        fold_rows.append({"fold_start": str(te_months[0]), "rule": best,
                          "train_mean": best_mu, "n_test": len(te_r),
                          "test_mean": te_r[ret_col].mean() if len(te_r) else np.nan})
        if len(te_r):
            oos.append(te_r)
        if not anchored:
            start_i += test_m

    if not oos:
        return {"error": "no out-of-sample trades"}
    o = pd.concat(oos).sort_values("dt")
    r = o[ret_col]
    eq = (1 + r / 100).cumprod()
    switches = sum(1 for a, b in zip(picks, picks[1:]) if a != b)
    return {
        "folds": pd.DataFrame(fold_rows), "oos": o, "eq": eq,
        "n": len(r), "mean": r.mean(), "median": r.median(),
        "win": (r > 0).mean(),
        "t": r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if r.std(ddof=1) > 0 else np.nan,
        "total": eq.iloc[-1] - 1, "maxdd": (eq / eq.cummax() - 1).min(),
        "picks": picks, "switches": switches,
        "stability": 1 - switches / max(len(picks) - 1, 1),
    }


def report(label: str, res: dict) -> None:
    if "error" in res:
        print(f"   {label:<34}{res['error']}")
        return
    print(f"   {label:<34}{res['n']:>7}{res['mean']:>+9.3f}{res['median']:>+9.3f}"
          f"{res['win'] * 100:>5.0f}%{res['t']:>+7.2f}{res['total'] * 100:>+10.1f}"
          f"{res['maxdd'] * 100:>+8.1f}{res['stability'] * 100:>9.0f}%"
          f"{len(set(res['picks'])):>7}")


HEADER = (f"   {'strategy':<34}{'OOS n':>7}{'mean %':>9}{'median':>9}{'win':>5}"
          f"{'t':>7}{'total %':>10}{'maxDD':>8}{'stability':>10}{'rules':>7}")
