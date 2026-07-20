"""Prefix-invariance test for the causal regime features.

Recompute ATR/DI/ADX on rows 0..k of a real instrument and assert row k is
bit-identical (rtol 1e-12) to the value produced on the full series.  Any
lookahead -- a centred window, a full-sample normalisation, a reverse fill --
breaks this immediately.

Run:  ../../../../.venv/bin/python -m pytest test_causality.py -q
      (or plain `python test_causality.py`)
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from regime_defs import add_causal_features, label_adx_regime, wilder_adx

HERE = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(HERE, "data", "regime", "daily.parquet")


def _series(u: str = "NIFTY") -> pd.DataFrame:
    d = pd.read_parquet(DAILY)
    g = d[d["underlying"] == u].sort_values("sidx").reset_index(drop=True)
    assert len(g) > 400, f"{u}: not enough history ({len(g)})"
    return g


def test_prefix_invariance_adx():
    g = _series()
    h, l, c = (g["h"].to_numpy(float), g["l"].to_numpy(float), g["c"].to_numpy(float))
    full = wilder_adx(h, l, c)
    for k in (40, 77, 150, 301, len(c) - 1):
        pref = wilder_adx(h[: k + 1], l[: k + 1], c[: k + 1])
        for name, fa, pa in zip(("atr", "pdi", "ndi", "adx"), full, pref):
            a, b = fa[k], pa[k]
            if np.isnan(a) and np.isnan(b):
                continue
            assert np.isclose(a, b, rtol=1e-12, atol=0.0), (name, k, a, b)


def test_prefix_invariance_frame_and_label():
    g = _series("BANKNIFTY")
    full = add_causal_features(g)
    full_lab = label_adx_regime(full)
    for k in (60, 200, 500):
        pref = add_causal_features(g.iloc[: k + 1].copy())
        pref_lab = label_adx_regime(pref)
        assert np.isclose(full["adx"].iloc[k], pref["adx"].iloc[k], rtol=1e-12, equal_nan=True)
        assert full_lab.iloc[k] is np.nan or full_lab.iloc[k] == pref_lab.iloc[k]


def test_regime_label_uses_no_future_bar():
    """Mutating a strictly FUTURE bar must not change today's label."""
    g = _series("NIFTY").copy()
    k = 400
    base = add_causal_features(g)["adx"].iloc[k]
    g2 = g.copy()
    g2.loc[g2.index[k + 5:], ["o", "h", "l", "c"]] *= 1.35
    pert = add_causal_features(g2)["adx"].iloc[k]
    assert np.isclose(base, pert, rtol=1e-12, equal_nan=True)


if __name__ == "__main__":
    test_prefix_invariance_adx()
    test_prefix_invariance_frame_and_label()
    test_regime_label_uses_no_future_bar()
    print("causality: OK")
