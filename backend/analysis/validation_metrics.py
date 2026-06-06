"""
Strategy-validation metrics — the six overfitting gates + Monte-Carlo.

Implements the methodology in docs/STRATEGY_TESTING_PLAN.md §4:

  Gate 1  OOS trade count        >= 100 (min 50)
  Gate 2  Walk-Forward Efficiency >= 0.50 (prefer >= 0.65)   [computed in walk_forward.py]
  Gate 3  Deflated Sharpe (DSR)   >= 0.40
  Gate 4  Prob. Backtest Overfit  <  0.40 (prefer < 0.25)
  Gate 5  Minimum Backtest Length  backtest length >= MinBTL(N, SR)
  Gate 6  5th-pct Monte-Carlo SR  >  0.30

Pure numpy/scipy — no app / DB / broker imports, so it runs anywhere (locally
off a DB pull, or in a sweep sidecar). All Sharpes here are *per-observation*
(per-trade or per-period) unless annualised explicitly; ratios (WFE) and the
PSR/DSR machinery are annualisation-invariant.

References: Bailey & López de Prado — "The Deflated Sharpe Ratio" (2014),
"The Probability of Backtest Overfitting" (2015, CSCV).
"""
from __future__ import annotations

import itertools
from typing import Iterable, Optional, Sequence

import numpy as np
from scipy import stats

EULER_MASCHERONI = 0.5772156649015329


# ──────────────────────────── basic stats ────────────────────────────

def sharpe(returns: Sequence[float], periods_per_year: Optional[float] = None) -> float:
    """Per-observation Sharpe; annualised if periods_per_year given."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return 0.0
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0
    s = r.mean() / sd
    return float(s * np.sqrt(periods_per_year)) if periods_per_year else float(s)


def equity_curve(returns: Sequence[float], start: float = 0.0) -> np.ndarray:
    return start + np.cumsum(np.asarray(returns, dtype=float))


def max_drawdown(equity: Sequence[float]) -> float:
    """Max drawdown in equity units (negative). Pass an equity curve."""
    e = np.asarray(equity, dtype=float)
    if e.size == 0:
        return 0.0
    peak = np.maximum.accumulate(e)
    return float((e - peak).min())


def profit_factor(returns: Sequence[float]) -> float:
    r = np.asarray(returns, dtype=float)
    gp = r[r > 0].sum()
    gl = -r[r < 0].sum()
    if gl > 0:
        return float(gp / gl)
    return float("inf") if gp > 0 else 0.0


def expectancy(returns: Sequence[float]) -> float:
    r = np.asarray(returns, dtype=float)
    return float(r.mean()) if r.size else 0.0


def win_rate(returns: Sequence[float]) -> float:
    r = np.asarray(returns, dtype=float)
    return float((r > 0).mean()) if r.size else 0.0


def summarize_returns(returns: Sequence[float]) -> dict:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    eq = equity_curve(r)
    return {
        "trades": int(r.size),
        "total": float(r.sum()),
        "expectancy": expectancy(r),
        "win_rate": win_rate(r),
        "profit_factor": profit_factor(r),
        "sharpe": sharpe(r),
        "max_drawdown": max_drawdown(eq),
        "skew": float(stats.skew(r)) if r.size > 2 else 0.0,
        "kurtosis": float(stats.kurtosis(r, fisher=False)) if r.size > 3 else 3.0,
    }


# ──────────────────── Probabilistic / Deflated Sharpe ────────────────────

def probabilistic_sharpe_ratio(returns: Sequence[float], sr_benchmark: float = 0.0) -> float:
    """PSR: P(true SR > sr_benchmark) given sample SR, length, skew, kurtosis."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    T = r.size
    if T < 3:
        return 0.0
    sr = sharpe(r)
    g3 = float(stats.skew(r))
    g4 = float(stats.kurtosis(r, fisher=False))  # non-excess kurtosis
    denom = 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr * sr
    if denom <= 0:
        return 0.0
    z = (sr - sr_benchmark) * np.sqrt(T - 1.0) / np.sqrt(denom)
    return float(stats.norm.cdf(z))


def expected_max_sharpe(
    n_trials: int,
    trial_sharpes: Optional[Sequence[float]] = None,
    sr_variance: Optional[float] = None,
) -> float:
    """Expected maximum Sharpe under the null over N independent trials
    (the deflation benchmark). Uses the cross-sectional variance of the trial
    Sharpes when available."""
    if sr_variance is None:
        if trial_sharpes is not None and len(trial_sharpes) > 1:
            sr_variance = float(np.var(np.asarray(trial_sharpes, dtype=float), ddof=1))
        else:
            sr_variance = 1.0
    N = max(int(n_trials), 2)
    ppf = stats.norm.ppf
    emax = np.sqrt(max(sr_variance, 0.0)) * (
        (1.0 - EULER_MASCHERONI) * ppf(1.0 - 1.0 / N)
        + EULER_MASCHERONI * ppf(1.0 - 1.0 / (N * np.e))
    )
    return float(emax)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    n_trials: int,
    trial_sharpes: Optional[Sequence[float]] = None,
) -> float:
    """DSR = PSR evaluated against the expected-max-Sharpe of N trials."""
    sr0 = expected_max_sharpe(n_trials=n_trials, trial_sharpes=trial_sharpes)
    return probabilistic_sharpe_ratio(returns, sr_benchmark=sr0)


def min_backtest_length_years(n_trials: int, target_sr_annual: float = 1.0) -> float:
    """Minimum track-record length (years) for the claimed annual Sharpe to be
    significant given N trials (López de Prado). MinBTL ≈ E[maxSR|unit-var,N]² / SR²."""
    if target_sr_annual <= 0:
        return float("inf")
    emax = expected_max_sharpe(n_trials=n_trials, sr_variance=1.0)
    return float(emax * emax / (target_sr_annual * target_sr_annual))


# ───────────────────── PBO via CSCV (López de Prado) ─────────────────────

def probability_of_backtest_overfitting(
    perf_matrix: np.ndarray,
    n_partitions: int = 14,
    max_combinations: int = 4000,
) -> dict:
    """Combinatorially-Symmetric Cross-Validation PBO.

    perf_matrix: (T observations × N strategies/param-sets) of per-period returns.
    Splits the T rows into `n_partitions` blocks; for every train/test split that
    takes half the blocks as IS and half as OOS, picks the IS-best strategy and
    measures its OOS rank. PBO = fraction of splits where the IS-best lands below
    the OOS median (logit < 0).
    """
    M = np.asarray(perf_matrix, dtype=float)
    if M.ndim != 2 or M.shape[1] < 2 or M.shape[0] < n_partitions:
        return {"pbo": float("nan"), "splits": 0, "note": "insufficient data"}
    T, N = M.shape
    S = n_partitions - (n_partitions % 2)  # even
    blocks = np.array_split(np.arange(T), S)

    def perf(idx: np.ndarray) -> np.ndarray:
        sub = M[idx]
        mu = sub.mean(axis=0)
        sd = sub.std(axis=0, ddof=1)
        sd[sd == 0] = np.nan
        sr = mu / sd
        return np.nan_to_num(sr, nan=-1e9)

    combos = list(itertools.combinations(range(S), S // 2))
    if len(combos) > max_combinations:
        rng = np.random.default_rng(0)
        combos = [combos[i] for i in rng.choice(len(combos), size=max_combinations, replace=False)]

    logits = []
    for is_blocks in combos:
        is_set = set(is_blocks)
        is_idx = np.concatenate([blocks[b] for b in range(S) if b in is_set])
        oos_idx = np.concatenate([blocks[b] for b in range(S) if b not in is_set])
        is_sr = perf(is_idx)
        oos_sr = perf(oos_idx)
        n_star = int(np.argmax(is_sr))
        # relative rank of n* in OOS (1 = worst .. N = best)
        order = np.argsort(oos_sr)
        rank = int(np.where(order == n_star)[0][0]) + 1
        w = rank / (N + 1.0)
        w = min(max(w, 1e-6), 1 - 1e-6)
        logits.append(np.log(w / (1.0 - w)))

    logits = np.asarray(logits, dtype=float)
    pbo = float((logits < 0).mean()) if logits.size else float("nan")
    return {"pbo": pbo, "splits": int(logits.size), "logit_mean": float(logits.mean()) if logits.size else float("nan")}


# ───────────────────────── Monte-Carlo / bootstrap ─────────────────────────

def monte_carlo_reshuffle(trade_returns: Sequence[float], n: int = 1000, seed: int = 7) -> dict:
    """Bootstrap (resample-with-replacement) the trade sequence; report the
    distribution of Sharpe / maxDD / final equity."""
    r = np.asarray(trade_returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 5:
        return {"note": "too few trades", "trades": int(r.size)}
    rng = np.random.default_rng(seed)
    T = r.size
    sharpes = np.empty(n)
    maxdds = np.empty(n)
    finals = np.empty(n)
    for i in range(n):
        samp = r[rng.integers(0, T, size=T)]
        sharpes[i] = sharpe(samp)
        finals[i] = samp.sum()
        maxdds[i] = max_drawdown(np.cumsum(samp))
    return {
        "trades": int(T),
        "sharpe_median": float(np.median(sharpes)),
        "sharpe_p05": float(np.percentile(sharpes, 5)),
        "sharpe_p10": float(np.percentile(sharpes, 10)),
        "final_median": float(np.median(finals)),
        "final_p05": float(np.percentile(finals, 5)),
        "maxdd_p95": float(np.percentile(maxdds, 5)),  # worst-case (most-negative) DD
        "maxdd_median": float(np.median(maxdds)),
    }


def bootstrap_ci(values: Sequence[float], stat=np.mean, n: int = 1000, alpha: float = 0.05, seed: int = 7) -> dict:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size < 3:
        return {"point": float(stat(v)) if v.size else float("nan"), "lo": float("nan"), "hi": float("nan")}
    rng = np.random.default_rng(seed)
    boot = np.array([stat(v[rng.integers(0, v.size, size=v.size)]) for _ in range(n)])
    return {
        "point": float(stat(v)),
        "lo": float(np.percentile(boot, 100 * alpha / 2)),
        "hi": float(np.percentile(boot, 100 * (1 - alpha / 2))),
    }


# ─────────────────────────── the six-gate report ───────────────────────────

def gate_report(
    oos_returns: Sequence[float],
    *,
    n_trials: int,
    trial_sharpes: Optional[Sequence[float]] = None,
    walk_forward_efficiency: Optional[float] = None,
    perf_matrix: Optional[np.ndarray] = None,
    backtest_years: Optional[float] = None,
    target_sr_annual: float = 1.0,
    regime_sharpes: Optional[dict] = None,
) -> dict:
    """Run all six gates on a strategy's combined OOS trade returns + sweep
    context. Returns metrics + per-gate pass/fail + overall verdict.

    `n_trials` = (#param sets) × (#WF windows) tested in the sweep — this is what
    deflates the claim, so log it honestly.
    """
    r = np.asarray(oos_returns, dtype=float)
    r = r[np.isfinite(r)]
    base = summarize_returns(r)

    dsr = deflated_sharpe_ratio(r, n_trials=n_trials, trial_sharpes=trial_sharpes) if r.size >= 3 else 0.0
    mc = monte_carlo_reshuffle(r)
    pbo = probability_of_backtest_overfitting(perf_matrix) if perf_matrix is not None else {"pbo": float("nan"), "splits": 0}
    min_btl = min_backtest_length_years(n_trials=n_trials, target_sr_annual=target_sr_annual)

    gates = {
        "g1_oos_trades": {"value": base["trades"], "threshold": ">=100 (min 50)", "pass": base["trades"] >= 50, "strong": base["trades"] >= 100},
        "g2_wfe": {"value": walk_forward_efficiency, "threshold": ">=0.50", "pass": (walk_forward_efficiency is None) or (walk_forward_efficiency >= 0.50), "strong": (walk_forward_efficiency or 0) >= 0.65},
        "g3_dsr": {"value": round(dsr, 4), "threshold": ">=0.40", "pass": dsr >= 0.40},
        "g4_pbo": {"value": pbo.get("pbo"), "threshold": "<0.40", "pass": (pbo.get("pbo") is None) or (np.isnan(pbo.get("pbo", np.nan))) or (pbo["pbo"] < 0.40), "strong": (not np.isnan(pbo.get("pbo", np.nan))) and pbo["pbo"] < 0.25},
        "g5_min_btl": {"value": round(min_btl, 2), "have_years": backtest_years, "threshold": "backtest>=MinBTL", "pass": (backtest_years is None) or (backtest_years >= min_btl)},
        "g6_mc_sharpe_p05": {"value": round(mc.get("sharpe_p05", float("nan")), 4) if "sharpe_p05" in mc else None, "threshold": ">0.30", "pass": mc.get("sharpe_p05", -1) > 0.30},
    }
    if regime_sharpes:
        ok = all(regime_sharpes.get(k, -1) >= t for k, t in (("low", 0.40), ("medium", 0.50), ("high", 0.30)))
        gates["g7_regime"] = {"value": regime_sharpes, "threshold": "low>=.40 med>=.50 high>=.30", "pass": ok}

    passed = all(g["pass"] for g in gates.values())
    return {
        "metrics": base,
        "deflated_sharpe": dsr,
        "monte_carlo": mc,
        "pbo": pbo,
        "min_backtest_length_years": min_btl,
        "n_trials": int(n_trials),
        "gates": gates,
        "verdict": "PASS" if passed else "FAIL",
        "gates_passed": int(sum(1 for g in gates.values() if g["pass"])),
        "gates_total": len(gates),
    }
