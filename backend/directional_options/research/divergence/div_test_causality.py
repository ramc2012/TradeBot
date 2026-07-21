"""(D) PREFIX-INVARIANCE proof for every divergence element.

A causal feature computed on the first k bars of a series must equal, to
rtol 1e-12, the same feature computed on the full series and then truncated to
k bars. Any lookahead -- a centred pivot read before its right wing closed, a
divergence anchored on a pivot that is not yet confirmed, a trendline drawn
through a future high -- breaks this identity immediately.

The pivot columns themselves (piv_low / piv_high) are DELIBERATELY excluded:
they are known to be non-causal AT their own index (that is what the R-wing
means). What must hold is that every CONSUMER of them (div, hl_conf, tl_break,
tl_recent) is causal, and that is what is asserted here.

Run:  ../../../../.venv/bin/python div_test_causality.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "cascade"))
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import div_defs as D  # noqa: E402

RTOL = 1e-12
# columns that must be prefix-invariant
CAUSAL_COLS = ["cross", "div", "div_price", "div_macd", "div_anchor", "div_prior",
               "div_any", "div_any_macd", "div_any_anchor",
               "str_hist", "str_slope", "str_below0", "str_thrust", "str_volz",
               "str_div_macd", "hl_conf", "hl_pivot", "hl_lift",
               "tl_break", "tl_slope", "tl_recent"]
# these are causal by construction only at index+PIV_R, never at index
NONCAUSAL_BY_DESIGN = ["piv_low", "piv_high"]


def check(g: pd.DataFrame, cuts: list[int]) -> list[str]:
    full = D.build_elements(g)
    bad = []
    for k in cuts:
        if k < 60 or k > len(g):
            continue
        pre = D.build_elements(g.iloc[:k])
        for c in CAUSAL_COLS:
            a = pre[c].to_numpy()
            b = full[c].to_numpy()[:k]
            if a.dtype == bool or b.dtype == bool:
                if not np.array_equal(a.astype(bool), b.astype(bool)):
                    n = int((a.astype(bool) != b.astype(bool)).sum())
                    bad.append(f"{c} cut={k} bool-mismatch n={n}")
                continue
            a = a.astype(float)
            b = b.astype(float)
            m = np.isfinite(a) | np.isfinite(b)
            if not np.array_equal(np.isfinite(a), np.isfinite(b)):
                bad.append(f"{c} cut={k} nan-pattern differs")
                continue
            m = np.isfinite(a)
            if m.any() and not np.allclose(a[m], b[m], rtol=RTOL, atol=0.0):
                d = np.max(np.abs(a[m] - b[m]) / np.maximum(np.abs(b[m]), 1e-300))
                bad.append(f"{c} cut={k} max_rel={d:.3e}")
    return bad


def main() -> int:
    import div_build as B
    e = pd.read_parquet(os.path.join(HERE, "data", "elem.parquet"))
    # the non-causal-by-design columns must indeed be present (so the test is
    # not silently passing on a panel that never had pivots)
    for c in NONCAUSAL_BY_DESIGN + CAUSAL_COLS:
        assert c in e.columns, c
    daily, _ = B.load_panel()
    names = ["PNB", "SBIN", "NIFTY", "BANKNIFTY", "TATASTEEL", "IDFCFIRSTB",
             "CANBK", "HINDALCO", "ONGC", "COALINDIA"]
    names = [n for n in names if n in set(daily["underlying"])]
    fails = []
    for u in names:
        g = daily[daily["underlying"] == u].sort_values("sidx").reset_index(drop=True)
        cuts = [80, 120, 180, 240, len(g) - 1, len(g)]
        bad = check(g, cuts)
        print(f"{u:12s} n={len(g):4d} cuts={len(cuts)} " + ("OK" if not bad else "FAIL"))
        for b in bad:
            print("   ", b)
        fails += bad
    print("\nPREFIX-INVARIANCE:", "PASS" if not fails else f"FAIL ({len(fails)})")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
