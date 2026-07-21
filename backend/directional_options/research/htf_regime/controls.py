"""(3-controls) CONTROL DESIGN — declared before measurement.

THE BASE-RATE TRAP THIS STUDY MUST NOT FALL INTO: over a 15-month broadly
rising sample, "daily uptrend + long CE" inherits market beta. Any timer
fired inside an up-regime will look good against an unconditional baseline
simply because the regime bars themselves drift up. Therefore:

C1  TIMER-UNFILTERED: the identical timer on ALL bars (regime ignored).
    Answers: does the daily filter lift the timer at all?
C2  RANDOM-INSIDE-REGIME (LOAD-BEARING): draws of random bars uniformly from
    the SAME regime-on bar universe, matched to each cell's entry count and
    per-underlying composition, expressed through the SAME option contract
    selection, holds and costs. 200 seeded draws -> a null distribution of
    the cell statistic; the cell's percentile against it is the decisive
    number. This control carries the full regime beta, so beating it is the
    only evidence the TIMER adds anything beyond being long in an up-market.
C3  REGIME-VALUE ISOLATION: C2 vs matched unconditional random bars.
    Attributes whatever remains to the regime itself (mostly beta by
    hypothesis; reported, not celebrated).

Interpretation rule (pre-registered): a cell is a POSITIVE finding only if
(a) it clears C1 AND (b) its C2 percentile >= 97.5 at the Bonferroni-
corrected level stated in study_grid.py. Beating C1 but not C2 = "the regime
is beta, the timer adds nothing" and is reported plainly as such.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

N_DRAWS = 200
SEED = 20260721


def random_inside_regime(regime_bars: pd.DataFrame, cell_entries: pd.DataFrame,
                         n_draws: int = N_DRAWS, seed: int = SEED):
    """Yield seeded matched random entry sets.

    regime_bars: universe of (underlying, time) bars where the given regime
        state is on (lag-1 governed), for the cell's timeframe.
    cell_entries: the cell's actual entries with an `underlying` column;
        the draw matches the per-underlying entry count exactly, so name
        composition (and hence name-level beta/vol) is held fixed.
    """
    rng = np.random.default_rng(seed)
    counts = cell_entries.groupby("underlying").size()
    pools = {u: g.reset_index(drop=True)
             for u, g in regime_bars.groupby("underlying")}
    for _ in range(n_draws):
        picks = []
        for u, k in counts.items():
            pool = pools.get(u)
            if pool is None or pool.empty:
                continue
            idx = rng.integers(0, len(pool), size=min(k, len(pool)))
            picks.append(pool.iloc[idx])
        yield pd.concat(picks, ignore_index=True) if picks else pd.DataFrame()


def percentile_vs_null(stat: float, null_stats: np.ndarray) -> float:
    """Fraction of null draws the observed statistic exceeds (0..1)."""
    null_stats = np.asarray(null_stats, float)
    return float((stat > null_stats).mean())
