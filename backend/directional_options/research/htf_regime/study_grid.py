"""STUDY GRID + MULTIPLICITY ACCOUNTING — enumerated in code so the count in
the report is auditable, not hand-waved.

PRIMARY PRE-REGISTERED CELL (declared before any measurement, tested alone
at alpha = 0.05): R1 x deep_macd x 30m x hold_1d x slight_ITM.
Everything else is grid, corrected.

Sensitivity parameters (DEEP_MIN grid, ADX threshold grid, rising-lookback
grid, VWAP-vs-EMA anchor, close-vs-next-open fill) are DESCRIPTIVE — they
are reported but never promoted to findings without entering this count.
"""
from __future__ import annotations

from itertools import product

REGIMES = ("r1", "r2")                       # regime_defs.py
TIMERS = ("deep_macd", "pullback_anchor", "orb", "macd_plain")
TIMEFRAMES = ("30m", "1h")
HOLDS = ("2h", "eod", "1d", "3d")            # all exits at bar closes;
                                             # 1d/3d exit at 15:15 IST t+n
MONEYNESS = ("ATM", "slight_ITM")
COMPARISONS_PER_CELL = 2                     # vs C1 unfiltered, vs C2 random-
                                             # inside-regime (the decisive one)

ALPHA = 0.05
FDR_Q = 0.10

PRIMARY_CELL = ("r1", "deep_macd", "30m", "1d", "slight_ITM")


def cells():
    return list(product(REGIMES, TIMERS, TIMEFRAMES, HOLDS, MONEYNESS))


def grid_size() -> dict:
    n_cells = len(cells())
    n_tests = n_cells * COMPARISONS_PER_CELL
    return {
        "cells": n_cells,                        # 2*4*2*4*2 = 128
        "comparisons_per_cell": COMPARISONS_PER_CELL,
        "total_tests": n_tests,                  # 256
        "bonferroni_alpha": ALPHA / n_tests,     # 1.953e-4
        "fdr_q": FDR_Q,
        "primary_cell": PRIMARY_CELL,            # alone at alpha=0.05
    }


if __name__ == "__main__":
    for k, v in grid_size().items():
        print(f"{k}: {v}")
