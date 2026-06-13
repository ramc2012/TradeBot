"""
Walk-forward + sweep harness (plan §4.1, §5). One call runs a lane through all
six gates + Monte-Carlo + regime conditioning.

A "lane" is supplied as:
  run_fn(frame: DataFrame, params: dict) -> result   # the lane's backtester
  extract_returns(result) -> list[float]             # per-trade R (or net PnL)
  extract_exit_times(result) -> list[timestamp]      # per-trade exit time (optional)

so this module stays lane-agnostic. Pure pandas/numpy + the metrics module.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

import numpy as np
import pandas as pd

from analysis import validation_metrics as vm

RunFn = Callable[[pd.DataFrame, dict], Any]
Extract = Callable[[Any], Sequence[float]]
ExtractTimes = Callable[[Any], Sequence]

# ── SACRED held-out OOS block ───────────────────────────────────────────────
# With 5 lanes x many parameter grids all hitting the same ~2yr of NSE data,
# family-wise overfit risk is high (this codebase has a 32/32-OOS-negative
# history). Reserve ONE terminal block that NO tuning / selection / sweep may
# read; evaluate a chosen config against it EXACTLY ONCE — that single number is
# the only defensible cross-lane go/no-go. Everything strictly before this date
# is the development set; everything on/after it is frozen.
HELD_OUT_START = pd.Timestamp("2026-04-01", tz="UTC")


def assert_tuning_window_safe(end_time, *, label: str = "tuning window") -> None:
    """Raise if a tuning/selection window reaches into the sacred held-out block.
    Call this with the latest timestamp any sweep/walk-forward is allowed to read."""
    t = pd.Timestamp(end_time)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    if t >= HELD_OUT_START:
        raise AssertionError(
            f"{label} ends at {t.isoformat()} which is on/after the sacred held-out "
            f"block start {HELD_OUT_START.date()} — tuning may not read held-out data."
        )


def split_development_heldout(frame: pd.DataFrame, *, time_col: str = "time"):
    """Split a frame into (development, held_out) at HELD_OUT_START. Tuning uses
    development only; the final verdict evaluates ONCE on held_out."""
    t = pd.to_datetime(frame[time_col], utc=True)
    return (
        frame[t < HELD_OUT_START].reset_index(drop=True),
        frame[t >= HELD_OUT_START].reset_index(drop=True),
    )


def _frame_between(frame: pd.DataFrame, t0, t1, time_col: str) -> pd.DataFrame:
    t = pd.to_datetime(frame[time_col], utc=True)
    return frame[(t >= t0) & (t < t1)].reset_index(drop=True)


def make_windows(frame: pd.DataFrame, *, time_col: str, is_days: int, oos_days: int, stride_days: int):
    t = pd.to_datetime(frame[time_col], utc=True)
    start, end = t.min(), t.max()
    windows = []
    is_start = start
    while True:
        is_end = is_start + timedelta(days=is_days)
        oos_end = is_end + timedelta(days=oos_days)
        if oos_end > end + timedelta(days=1):
            break
        windows.append((is_start, is_end, is_end, oos_end))
        is_start = is_start + timedelta(days=stride_days)
    return windows


def grid_iter(grid: dict):
    """Cartesian product of a {param: [values]} grid → list of param dicts."""
    import itertools

    keys = list(grid.keys())
    for combo in itertools.product(*[grid[k] for k in keys]):
        yield dict(zip(keys, combo))


def sweep_full(
    frame: pd.DataFrame,
    run_fn: RunFn,
    grid: dict,
    extract_returns: Extract,
    extract_exit_times: Optional[ExtractTimes] = None,
    *,
    time_col: str = "time",
) -> dict:
    """Run every param set over the FULL frame → trial Sharpes (for DSR) and a
    per-day PnL matrix (for CSCV-PBO)."""
    param_sets = list(grid_iter(grid))
    trial_sharpes = []
    daily_cols = {}
    rows_summary = []
    for i, p in enumerate(param_sets):
        res = run_fn(frame, p)
        rets = np.asarray(list(extract_returns(res)), dtype=float)
        trial_sharpes.append(vm.sharpe(rets))
        rows_summary.append({**p, **vm.summarize_returns(rets)})
        if extract_exit_times is not None and rets.size:
            times = pd.to_datetime(pd.Series(list(extract_exit_times(res))), utc=True)
            n = min(len(times), len(rets))
            s = pd.Series(rets[:n], index=times[:n].dt.floor("D"))
            daily_cols[i] = s.groupby(level=0).sum()
    perf_matrix = None
    if daily_cols:
        mat = pd.DataFrame(daily_cols).sort_index().fillna(0.0)
        if mat.shape[0] >= 8 and mat.shape[1] >= 2:
            perf_matrix = mat.to_numpy()
    return {
        "param_sets": param_sets,
        "trial_sharpes": trial_sharpes,
        "perf_matrix": perf_matrix,
        "summary_table": rows_summary,
    }


def walk_forward(
    frame: pd.DataFrame,
    run_fn: RunFn,
    grid: dict,
    extract_returns: Extract,
    extract_exit_times: Optional[ExtractTimes] = None,
    *,
    time_col: str = "time",
    is_days: int = 270,
    oos_days: int = 90,
    stride_days: int = 30,
    select: str = "total",  # IS selection metric: total | sharpe | expectancy
    min_is_trades: int = 10,
) -> dict:
    """Rolling IS→OOS. Per window: fit the grid on IS, pick the IS-best param set,
    evaluate it on the held-out OOS. Returns combined OOS returns + per-window WFE."""
    windows = make_windows(frame, time_col=time_col, is_days=is_days, oos_days=oos_days, stride_days=stride_days)
    param_sets = list(grid_iter(grid))
    per_window = []
    oos_all: list[float] = []
    oos_times_all: list = []

    def score(rets: np.ndarray) -> float:
        if rets.size < min_is_trades:
            return -1e9
        if select == "sharpe":
            return vm.sharpe(rets)
        if select == "expectancy":
            return vm.expectancy(rets)
        return float(rets.sum())

    for (is0, is1, oo0, oo1) in windows:
        is_frame = _frame_between(frame, is0, is1, time_col)
        oos_frame = _frame_between(frame, oo0, oo1, time_col)
        if len(is_frame) < 50 or len(oos_frame) < 20:
            continue
        best_p, best_s, best_is_rets = None, -1e18, np.array([])
        for p in param_sets:
            rets = np.asarray(list(extract_returns(run_fn(is_frame, p))), dtype=float)
            sc = score(rets)
            if sc > best_s:
                best_s, best_p, best_is_rets = sc, p, rets
        if best_p is None:
            continue
        oos_res = run_fn(oos_frame, best_p)
        oos_rets = np.asarray(list(extract_returns(oos_res)), dtype=float)
        is_sr = vm.sharpe(best_is_rets)
        oos_sr = vm.sharpe(oos_rets)
        wfe = (oos_sr / is_sr) if is_sr > 0 else (0.0 if oos_sr <= 0 else 1.0)
        per_window.append({
            "is_start": str(is0.date()), "oos_window": f"{oo0.date()}→{oo1.date()}",
            "best_params": best_p, "is_sharpe": round(is_sr, 3), "oos_sharpe": round(oos_sr, 3),
            "wfe": round(wfe, 3), "oos_trades": int(oos_rets.size), "oos_total": round(float(oos_rets.sum()), 2),
        })
        oos_all.extend(oos_rets.tolist())
        if extract_exit_times is not None and oos_rets.size:
            ts = list(extract_exit_times(oos_res))
            oos_times_all.extend(ts[: oos_rets.size])

    wfes = [w["wfe"] for w in per_window]
    oos_sharpes = [w["oos_sharpe"] for w in per_window]
    pbo_proxy = float(np.mean([s < np.median(oos_sharpes) for s in oos_sharpes])) if len(oos_sharpes) >= 4 else None
    return {
        "n_windows": len(per_window),
        "per_window": per_window,
        "oos_returns": oos_all,
        "oos_exit_times": oos_times_all,
        "wfe_median": float(np.median(wfes)) if wfes else None,
        "wfe_p10": float(np.percentile(wfes, 10)) if wfes else None,
        "pbo_window_proxy": pbo_proxy,
    }


def validate_strategy(
    frame: pd.DataFrame,
    run_fn: RunFn,
    grid: dict,
    extract_returns: Extract,
    *,
    extract_exit_times: Optional[ExtractTimes] = None,
    daily_closes: Optional[pd.Series] = None,
    time_col: str = "time",
    is_days: int = 270,
    oos_days: int = 90,
    stride_days: int = 30,
    select: str = "total",
    target_sr_annual: float = 1.0,
) -> dict:
    """End-to-end: sweep (DSR/PBO context) + walk-forward (WFE + OOS) → six-gate report."""
    t = pd.to_datetime(frame[time_col], utc=True)
    backtest_years = float((t.max() - t.min()).days) / 365.25

    full = sweep_full(frame, run_fn, grid, extract_returns, extract_exit_times, time_col=time_col)
    wf = walk_forward(
        frame, run_fn, grid, extract_returns, extract_exit_times,
        time_col=time_col, is_days=is_days, oos_days=oos_days, stride_days=stride_days, select=select,
    )

    n_trials = max(1, len(full["param_sets"]) * max(1, wf["n_windows"]))

    regime_sharpes = None
    if daily_closes is not None and wf["oos_exit_times"] and len(wf["oos_returns"]) == len(wf["oos_exit_times"]):
        try:
            from analytics.regime_hmm import label_daily_regimes, regime_for_timestamps, regime_conditional_sharpe

            daily_closes = pd.Series(daily_closes)
            daily_closes.index = pd.to_datetime(pd.Series(daily_closes.index)).dt.date
            labels = label_daily_regimes(daily_closes)
            regs = regime_for_timestamps(pd.Series(wf["oos_exit_times"]), labels)
            regime_sharpes = {k: v for k, v in regime_conditional_sharpe(pd.Series(wf["oos_returns"]), regs).items() if not k.endswith("_n")}
        except Exception:
            regime_sharpes = None

    report = vm.gate_report(
        wf["oos_returns"],
        n_trials=n_trials,
        trial_sharpes=full["trial_sharpes"],
        walk_forward_efficiency=wf["wfe_median"],
        perf_matrix=full["perf_matrix"],
        backtest_years=backtest_years,
        target_sr_annual=target_sr_annual,
        regime_sharpes=regime_sharpes,
    )
    report["walk_forward"] = {k: wf[k] for k in ("n_windows", "wfe_median", "wfe_p10", "pbo_window_proxy", "per_window")}
    report["sweep"] = {"n_param_sets": len(full["param_sets"]), "summary_table": full["summary_table"]}
    report["backtest_years"] = round(backtest_years, 2)
    return report
