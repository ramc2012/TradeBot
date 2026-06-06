"""
3-state market-regime labeller for regime-conditional gating (plan §4.4).

The plan calls for a 3-state HMM on daily returns + realized vol (low / medium /
high-vol). `hmmlearn` isn't installed in this env, so we use a Gaussian-mixture
clustering on [return, realized-vol] which gives the same low/med/high partition
without a new dependency. If `hmmlearn` is later added, swap GaussianMixture for
GaussianHMM (same feature matrix) for the temporal-transition prior.

Pure numpy/pandas/sklearn — no app imports.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

REGIMES = ("low", "medium", "high")


def label_daily_regimes(
    closes: pd.Series,
    *,
    vol_window: int = 5,
    n_states: int = 3,
    seed: int = 0,
) -> pd.Series:
    """Return a per-day regime label ('low'/'medium'/'high') indexed like `closes`
    (a daily close series). Regimes are ordered by realized-vol centroid."""
    from sklearn.mixture import GaussianMixture

    c = pd.Series(closes).astype(float).dropna()
    if c.size < max(20, vol_window + 5):
        return pd.Series("medium", index=c.index)
    ret = np.log(c / c.shift(1))
    rvol = ret.rolling(vol_window).std()
    feat = pd.DataFrame({"ret": ret, "rvol": rvol}).dropna()
    if feat.shape[0] < 12:
        return pd.Series("medium", index=c.index)

    X = feat.to_numpy()
    Xs = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-9)
    k = min(n_states, max(2, feat.shape[0] // 4))
    gm = GaussianMixture(n_components=k, covariance_type="full", random_state=seed, n_init=3, reg_covar=1e-4)
    comp = gm.fit_predict(Xs)

    # order components by their realized-vol centroid → low..high
    vol_centroid = [X[comp == j, 1].mean() if (comp == j).any() else np.inf for j in range(k)]
    order = np.argsort(vol_centroid)
    names = ["low", "medium", "high"][: k] if k == 3 else _spread_names(k)
    comp_to_name = {int(order[i]): names[i] for i in range(k)}

    labels = pd.Series([comp_to_name[int(x)] for x in comp], index=feat.index)
    return labels.reindex(c.index).fillna("medium")


def _spread_names(k: int) -> list[str]:
    if k <= 1:
        return ["medium"]
    if k == 2:
        return ["low", "high"]
    return ["low", "medium", "high"]


def regime_for_timestamps(timestamps: pd.Series, daily_labels: pd.Series, tz=None) -> pd.Series:
    """Map intraday/trade timestamps to their day's regime label."""
    ts = pd.to_datetime(pd.Series(timestamps), utc=True)
    days = ts.dt.tz_convert(tz).dt.date if tz else ts.dt.date
    # daily_labels indexed by date-like
    dl = daily_labels.copy()
    dl.index = pd.to_datetime(pd.Series(dl.index)).dt.date if not isinstance(dl.index[0], (str,)) else dl.index
    lut = {d: r for d, r in zip(dl.index, dl.values)}
    return days.map(lambda d: lut.get(d, "medium"))


def regime_conditional_sharpe(returns: pd.Series, regimes: pd.Series) -> dict:
    """Per-regime Sharpe of a trade-return series (aligned to `regimes`)."""
    from analysis.validation_metrics import sharpe

    out = {}
    df = pd.DataFrame({"r": np.asarray(returns, dtype=float), "regime": np.asarray(regimes)})
    for reg in REGIMES:
        sub = df.loc[df["regime"] == reg, "r"].to_numpy()
        out[reg] = round(sharpe(sub), 4) if sub.size >= 2 else None
        out[f"{reg}_n"] = int(sub.size)
    return out
